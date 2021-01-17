import asyncio
from h2.connection import H2Connection
from hyperframe.frame import SettingsFrame
from urllib.parse import urlparse, parse_qs
import socket
from h2.events import ResponseReceived
import ssl
import time
import string
import itertools
import logging


class H2Protocol(asyncio.Protocol):
    def __init__(self, settings: dict, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.loop = loop
        self.transport: asyncio.Transport = None
        self.peername = None
        self.socket = None
        self.h2conn = H2Connection()
        self.logger = logging.getLogger(self.__class__.__name__)

        self.connection_open = False

        self._sent_streams = {}
        self._settings = settings
        self._goaway_waiter = None

    def connection_made(self, transport: asyncio.Transport) -> None:
        self.connection_open = True
        self.peername = transport.get_extra_info('peername')
        self.socket = transport.get_extra_info('socket')
        ssl_socket = transport.get_extra_info('ssl_object')
        negotiated_protocol = ssl_socket.selected_alpn_protocol()
        if negotiated_protocol is None:
            negotiated_protocol = ssl_socket.selected_npn_protocol()
        if negotiated_protocol != "h2":
            raise RuntimeError("The server does not support HTTP/2")
        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.transport = transport

        self.h2conn.initiate_connection()
        self.h2conn.update_settings(self._settings)
        self.write_all()

    def data_received(self, data: bytes) -> None:
        events = self.h2conn.receive_data(data)
        for e in events:
            if isinstance(e, ResponseReceived):
                self.receive_response(e.headers, e.stream_id)

    def write_all(self):
        data = self.h2conn.data_to_send()
        self.logger.debug('Sending %d bytes' % len(data))
        self.transport.write(data)

    def connection_lost(self, exc):
        self.connection_open = False
        self.remove_all_unacknowleged_streams()
        self.logger.info("Connection to %s closed" % self.peername[0])
        if self._goaway_waiter:
            self._goaway_waiter.set_result(None)

    async def terminate(self):
        self.h2conn.close_connection()
        self.write_all()
        self.transport.close()

        waiter = self.loop.create_future()
        self._goaway_waiter = waiter
        await waiter

    def send_request(self, headers, end_stream=True):
        stream_id = self.h2conn.get_next_available_stream_id()
        self.h2conn.send_headers(stream_id, headers, end_stream=end_stream)
        self._sent_streams[stream_id] = self.loop.create_future()
        return stream_id

    def send_request_pair(self, headers1: list, headers2: list, data1: str = '', data2: str =''):
        max_payload_len = 1200
        data1 = bytearray(data1, encoding='utf-8')
        data2 = bytearray(data2, encoding='utf-8')

        # overestimation of headers: not taking HPACK into account
        headers1_len = len(' '.join(['%s: %s' % x for x in headers1]))
        headers2_len = len(' '.join(['%s: %s' % x for x in headers2]))
        if headers2_len > max_payload_len:
            self.logger.warning('Size of headers of second request may be larger than what fits in a single TCP packet - both request may not arrive at the same time')

        payload_len = len(data1) + len(data2) + headers1_len + headers2_len
        if len(data1) + len(data2) == 0:
            stream1 = self.send_request(headers1, end_stream=True)
            stream2 = self.send_request(headers2, end_stream=True)
        elif payload_len <= max_payload_len:
            stream1 = self.send_request(headers1, end_stream=False)
            stream2 = self.send_request(headers2, end_stream=False)
            self.h2conn.send_data(stream1, data1, end_stream=True)
            self.h2conn.send_data(stream2, data2, end_stream=True)
        else:
            if len(data1) == 0:
                stream2 = self.send_request(headers2, end_stream=False)
                self.h2conn.send_data(stream2, data2[:-1], end_stream=False)
                self.write_all()
                stream1 = self.send_request(headers1, end_stream=True)
                self.h2conn.send_data(stream2, data2[-1:], end_stream=True)
            elif len(data2) == 0:
                stream1 = self.send_request(headers1, end_stream=False)
                self.h2conn.send_data(stream1, data1[:-1], end_stream=False)
                self.write_all()
                self.h2conn.send_data(stream1, data1[-1:], end_stream=True)
                stream2 = self.send_request(headers2, end_stream=True)
            else:
                stream1 = self.send_request(headers1, end_stream=False)
                stream2 = self.send_request(headers2, end_stream=False)
                self.h2conn.send_data(stream1, data1[:-1], end_stream=False)
                self.h2conn.send_data(stream2, data2[:-1], end_stream=False)
                self.write_all()
                self.h2conn.send_data(stream1, data1[-1:], end_stream=True)
                self.h2conn.send_data(stream2, data2[-1:], end_stream=True)

        self.write_all()
        return stream1, stream2

    def receive_response(self, headers, stream_id):
        resp_time = time.time_ns()
        status_headers = list(filter(lambda x: x[0] == b':status', headers))
        status = '-1'
        if len(status_headers) == 1:
            status = status_headers[0][1].decode('utf-8')
        self._sent_streams[stream_id].set_result((status, resp_time))

    async def wait_for_all_responses(self, timeout):
        done, pending = await asyncio.wait([x for x in self._sent_streams.values()], timeout=timeout)
        for fut in pending:
            fut.set_result(None)

    def get_response_info(self, stream_id1, stream_id2):
        resp_info1 = self._sent_streams[stream_id1].result()
        resp_info2 = self._sent_streams[stream_id2].result()
        if resp_info1 is None or resp_info2 is None:
            return None
        # diff,status1,status2
        return resp_info2[1] - resp_info1[1], resp_info1[0], resp_info2[0]

    def remove_all_unacknowleged_streams(self):
        for stream_id, fut in self._sent_streams.items():
            if not fut.done():
                fut.set_result(None)


class H2Request:
    def __init__(self, method: str, url: str, headers: dict = None, data: str = ''):
        self.method = method
        self.url = self.scheme = self.host = self.port = self.path = self.query = None
        self.set_url(url)
        self.padding_params = ""
        self.data = data
        self.headers = headers if headers is not None else {}
        self.num_padding_params = 0

    def set_url(self, url: str):
        self.url = url
        o = urlparse(url)
        self.scheme = o.scheme
        self.host = o.netloc
        self.port = o.port or 443 if self.scheme == 'https' else 80
        self.path = o.path
        self.query = o.query

    def set_header(self, key: str, value: str):
        self.headers[key] = value

    def set_headers(self, headers: dict):
        [self.set_header(k, v) for k, v in headers.items()]

    def remove_header(self, key: str):
        del self.headers[key]

    def get_request_headers(self, include_padding_params=False):
        path = self.path
        if self.query != '':
            path += '?' + self.query
        headers = [
            (':method', self.method),
            (':authority', self.host),
            (':scheme', self.scheme),
            (':path', path + (self.padding_params if include_padding_params else '')),
        ]
        for k, v in self.headers.items():
            headers.append((k, v))
        return headers

    def set_num_padding_params(self, num: int):
        self.num_padding_params = num

    def create_padding_params(self, num: int):
        self.set_num_padding_params(num)
        self.padding_params = "?" if self.query == '' else "&"
        self.padding_params += self.gen_params()

    def gen_params(self):
        exclude_params = parse_qs(self.query).keys()
        new_params = []
        charset = string.ascii_lowercase + string.digits
        param_length = 1
        while len(new_params) < self.num_padding_params:
            new_params.extend([''.join(x) for x in itertools.combinations_with_replacement(charset, param_length) if ''.join(x) not in exclude_params])
            param_length += 1
        new_params = new_params[:self.num_padding_params]
        return '&'.join(new_params)


class H2Time:
    def __init__(self, request1: H2Request, request2: H2Request, send_order_pattern="12", sequential=True, num_request_pairs=50, inter_request_time_ms=10, num_padding_params=40, timeout=5):
        self.request1 = request1
        self.request2 = request2
        self.request1.create_padding_params(num_padding_params)
        self.request2.create_padding_params(num_padding_params)
        self.scheme = request1.scheme
        self.host = request1.host
        self.port = request1.port
        self.loop = asyncio.get_event_loop()
        self._settings = {SettingsFrame.HEADER_TABLE_SIZE: 4096}
        self.protocol: H2Protocol = None
        self.sequential = sequential
        self.send_order_pattern = send_order_pattern
        self.num_request_pairs = num_request_pairs
        self.inter_request_time_ms = inter_request_time_ms
        self.num_padding_params = num_padding_params
        self.sent_requests = []
        self.timeout = timeout

    async def __aenter__(self):
        _, self.protocol = await self.loop.create_connection(
            lambda: H2Protocol(self._settings, self.loop), self.host, self.port, ssl=H2Time._get_http2_ssl_context() if self.scheme == 'https' else None, family=socket.AF_INET
        )
        return self

    async def __aexit__(self, *exc):
        await self.terminate()

    async def terminate(self):
        if self.protocol:
            await self.protocol.terminate()
            self.protocol = None

    def send_request_pair(self, request_number):
        reverse_order = self.send_order_pattern[request_number % len(self.send_order_pattern)] == '2'

        headers1 = self.request1.get_request_headers(not reverse_order)
        headers2 = self.request2.get_request_headers(reverse_order)

        if reverse_order:
            stream_id2, stream_id1 = self.protocol.send_request_pair(headers2, headers1, self.request2.data, self.request1.data)
        else:
            stream_id1, stream_id2 = self.protocol.send_request_pair(headers1, headers2, self.request1.data, self.request2.data)

        self.sent_requests.append((stream_id1, stream_id2))

    @staticmethod
    def _get_http2_ssl_context():
        ctx = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
        ctx.options |= (
                ssl.OP_NO_SSLv2 | ssl.OP_NO_SSLv3 | ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1
        )
        ctx.options |= ssl.OP_NO_COMPRESSION
        ctx.set_ciphers("ECDHE+AESGCM:ECDHE+CHACHA20:DHE+AESGCM:DHE+CHACHA20")
        ctx.set_alpn_protocols(["h2", "http/1.1"])
        try:
            ctx.set_npn_protocols(["h2", "http/1.1"])
        except NotImplementedError:
            pass
        return ctx

    async def run_attack(self):
        for request_number in range(self.num_request_pairs):
            time.sleep(self.inter_request_time_ms / 1000)
            if not self.protocol.connection_open:
                break
            self.send_request_pair(request_number)
            if self.sequential:
                await self.protocol.wait_for_all_responses(self.timeout)

        await self.protocol.wait_for_all_responses(self.timeout)
        results = list(filter(lambda x: x is not None, map(lambda x: self.protocol.get_response_info(*x), self.sent_requests)))
        return results
