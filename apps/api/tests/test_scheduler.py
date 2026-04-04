from datetime import datetime, timezone

from app.api import routes
from app.services import scheduler


class _DetachedRun:
    def __init__(self):
        self._detached = False
        self._id = 91
        self._status = "completed"
        self._records_processed = 128
        self._finished_at = datetime(2026, 4, 3, 18, 15, tzinfo=timezone.utc)

    def detach(self):
        self._detached = True

    def _read(self, value):
        if self._detached:
            raise RuntimeError("Run detached from session")
        return value

    @property
    def id(self):
        return self._read(self._id)

    @property
    def status(self):
        return self._read(self._status)

    @property
    def records_processed(self):
        return self._read(self._records_processed)

    @property
    def finished_at(self):
        return self._read(self._finished_at)


class _SessionContext:
    def __init__(self, run):
        self.run = run

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.run.detach()

    def commit(self):
        return None


def test_run_refresh_cycle_now_returns_snapshot_before_session_detaches(monkeypatch):
    run = _DetachedRun()
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: _SessionContext(run))
    monkeypatch.setattr(scheduler, "run_refresh_cycle", lambda db, **kwargs: run)
    monkeypatch.setattr(scheduler, "schedule_event_refreshes", lambda: None)

    snapshot = scheduler.run_refresh_cycle_now(reason="manual")

    assert snapshot is not None
    assert snapshot.run_id == 91
    assert snapshot.status == "completed"
    assert snapshot.records_processed == 128
    assert snapshot.finished_at == datetime(2026, 4, 3, 18, 15, tzinfo=timezone.utc)


def test_health_endpoint_uses_sanitized_refresh_error_message(client, monkeypatch):
    raw_error = (
        "Instance <Run at 0x10cdd5df0> is not bound to a Session; "
        "attribute refresh operation cannot proceed "
        "(Background on this error at: https://sqlalche.me/e/20/bhk3)"
    )
    monkeypatch_payload = {
        "refresh_status": "failed",
        "refresh_reason": "manual",
        "last_successful_refresh_at": None,
        "data_stale": True,
        "refresh_error_message": scheduler.summarize_refresh_error_message(raw_error),
        "prop_refresh_status": "running",
        "prop_refresh_reason": "interval",
        "last_prop_refresh_at": None,
        "prop_data_stale": True,
        "prop_refresh_error_message": None,
        "active_refresh_job": None,
        "latest_refresh_job": None,
        "active_prop_refresh_job": None,
        "latest_prop_refresh_job": None,
    }
    monkeypatch.setattr(routes, "get_refresh_runtime_state", lambda: monkeypatch_payload)
    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["refresh_error_message"] == "The latest refresh hit a temporary database session issue."
    assert "sqlalche.me" not in payload["refresh_error_message"]
    assert payload["prop_refresh_status"] == "running"
    assert payload["prop_refresh_reason"] == "interval"
