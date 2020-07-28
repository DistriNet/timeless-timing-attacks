## h2time

`h2time.py` is a Python implementation that can be used to test HTTP/2 servers for [Timeless Timing Attack](https://tom.vg/papers/timeless-timing-attack_usenix2020.pdf) vulnerabilities.

### Requirements

* Python 3.7.x or higher - tested with Python 3.8.5
* The [hyper-h2](https://github.com/python-hyper/hyper-h2) Python package (`pip install h2`) - tested with 3.2.0
* OpenSSL

### Usage

A very basic example is given below, for additonal examples, please refer to [examples.py](examples.py).

```python
from h2time import H2Request, H2Time

r1 = H2Request('GET', 'https://tom.vg/?1')
r2 = H2Request('GET', 'https://tom.vg/?2')
async with H2Time(r1, r2) as h2t:
    results = await h2t.run_attack()
    print('\n'.join(map(lambda x: ','.join(map(str, x)), results)))
```

First two `H2Request` objects are created, which are then passed on to `H2Time`.
Note that both requests should be to the same server (as this is the basic requirement to perform timeless timing attacks).
When the `run_attack()` method is called, the client will start sending request-pairs and will try to ensure that both arrive at the same time at the server (the final bytes of each request should be placed in a single TCP packet).
On the first request, additional parameters are added to the URL to offset the difference in time when requests can start being processed (the number is defined by the `num_padding_params` parameter - default: 40).

`H2Time` can operate in a sequential mode, where it waits to send the next request-pair until the response for the previous one has been received.
When the `sequential` is set to `False`, all request-pairs will be sent at once, at an interval of a number of milliseconds defined by the `inter_request_time_ms` parameter.

The results that are returned is a list of tuples with 3 elements: (0) difference of response time (in nanoseconds) between the second request and the first one, (1): response status of the first request, (2): response status of the second request.

If the difference in response time is negative, this means that a response for the second request was received first.
To perform a timeless timing attack, one should only need to take into account whether the result is positive or negative (positive indicates that the processing time of the first request takes less time than processing the second request). 

### Timing attack best practices

Timing attacks can be quite tricky to exploit, so it's best to follow these best practices:

* Alternate between choosing which request to send first: change between `H2Time(r1, r2)` and `H2Time(r2, r1)` to avoid bias that may be introduced by the first request (support for this in `h2time.py` is planned)
* The number of request parameters that are needed may be server-dependent, so it's best to first experiment with what values work best (for 2 requests that have the same processing time, the distribution of positive & negative timing result should be 50/50)

### A word of caution

Please be aware that this Python implementation may still be a bit rough around the edges.
As it will be further developed, it is likely that there will be breaking changes.
If you encounter any issue with it, please [file an issue](https://github.com/DistriNet/timeless-timing-attacks/issues/new)!
For any other questions, suggestions and remarks, feel free to [contact me](https://twitter.com/tomvangoethem).
