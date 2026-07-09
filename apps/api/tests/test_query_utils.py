"""Regression tests for the chunked IN-clause helper and the naive/aware
datetime fix that the SQLite-variable crash used to mask."""

from datetime import datetime, timedelta, timezone

import pytest

from app.models import Market
from app.query_utils import IN_CHUNK_SIZE, chunked
from app.services.trade_desk import _time_to_close_minutes


def test_chunked_splits_and_preserves_order():
    assert list(chunked([1, 2, 3, 4, 5], size=2)) == [[1, 2], [3, 4], [5]]


def test_chunked_empty_yields_nothing():
    assert list(chunked([])) == []


def test_chunked_under_size_is_single_chunk():
    assert list(chunked(range(3), size=10)) == [[0, 1, 2]]


def test_chunked_large_list_stays_under_default_size():
    values = list(range(2100))
    batches = list(chunked(values))
    assert all(len(b) <= IN_CHUNK_SIZE for b in batches)
    assert [v for b in batches for v in b] == values  # nothing dropped/duplicated


def test_chunked_rejects_nonpositive_size():
    with pytest.raises(ValueError):
        list(chunked([1, 2], size=0))


def test_time_to_close_minutes_handles_naive_close_time():
    # SQLite returns naive datetimes even for DateTime(timezone=True) columns;
    # the helper must not raise "can't subtract offset-naive and offset-aware".
    naive_future = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(tzinfo=None)
    minutes = _time_to_close_minutes(Market(close_time=naive_future))
    assert isinstance(minutes, int)
    assert 100 <= minutes <= 130  # ~120 minutes to close


def test_time_to_close_minutes_naive_past_clamps_to_zero():
    naive_past = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(tzinfo=None)
    assert _time_to_close_minutes(Market(close_time=naive_past), now=datetime.now(timezone.utc)) == 0


def test_time_to_close_minutes_none_close_time():
    assert _time_to_close_minutes(Market(close_time=None)) is None
