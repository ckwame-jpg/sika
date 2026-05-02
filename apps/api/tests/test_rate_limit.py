import time

import pytest

from app.clients import _rate_limit


@pytest.fixture(autouse=True)
def _reset_registry():
    _rate_limit.reset_for_tests()
    yield
    _rate_limit.reset_for_tests()


def test_shared_bucket_returns_singleton_for_same_name():
    a = _rate_limit.shared_bucket("foo", rps=10.0, burst=5.0)
    b = _rate_limit.shared_bucket("foo", rps=999.0, burst=999.0)
    assert a is b


def test_shared_bucket_separates_by_name():
    a = _rate_limit.shared_bucket("foo", rps=10.0, burst=5.0)
    b = _rate_limit.shared_bucket("bar", rps=10.0, burst=5.0)
    assert a is not b


def test_token_bucket_burst_and_steady_state():
    bucket = _rate_limit.TokenBucket(rate_per_second=100.0, burst=3.0)
    start = time.monotonic()
    bucket.acquire()
    bucket.acquire()
    bucket.acquire()
    burst_elapsed = time.monotonic() - start
    assert burst_elapsed < 0.05  # burst tokens consumed instantly

    bucket.acquire()
    after_refill = time.monotonic() - start
    # 4th token must wait at least ~1/100s for refill
    assert after_refill >= 0.005


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("3", 3.0),
        ("0", 0.0),
        ("12.5", 12.5),
        ("  7  ", 7.0),
        (None, None),
        ("", None),
        ("not-a-number", None),
        ("-5", None),
    ],
)
def test_parse_retry_after(raw, expected):
    assert _rate_limit.parse_retry_after(raw) == expected
