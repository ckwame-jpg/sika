"""Bug #21: the ML weekly retrain moved from an APScheduler entry inside
the FastAPI process to ``.github/workflows/ml-retrain.yml``. The scheduler
side is guarded by ``test_scheduler.py::test_scheduler_does_not_register_weekly_model_retrain_job``.
This file guards the workflow file itself — it exists, runs on cron, and
calls the right ``ml.cli`` invocation. Plain-string assertions so the
test doesn't pull in PyYAML just for one file."""

from __future__ import annotations

from pathlib import Path


WORKFLOW_PATH = (
    Path(__file__).resolve().parents[3] / ".github" / "workflows" / "ml-retrain.yml"
)


def _workflow_text() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def test_workflow_file_exists():
    assert WORKFLOW_PATH.exists(), (
        f"Bug #21 moved weekly retraining here; expected {WORKFLOW_PATH} to exist."
    )


def test_workflow_runs_on_sunday_cron_and_manual_dispatch():
    """The old in-API job ran Sunday 03:00 UTC. Keep that cadence so
    operators don't have to relearn the schedule, and add
    ``workflow_dispatch`` so the same workflow can be triggered ad-hoc."""
    text = _workflow_text()
    assert 'cron: "0 3 * * 0"' in text or "cron: '0 3 * * 0'" in text, (
        "Expected a Sunday 03:00 UTC cron entry (``0 3 * * 0``)."
    )
    assert "workflow_dispatch" in text, (
        "Operators need to be able to retrigger retraining manually "
        "(e.g. after a data correction)."
    )


def test_workflow_invokes_ml_cli_train():
    """The training command is the contract between the workflow and
    ``apps/ml/ml/cli.py``. If someone moves the cli or renames the
    subcommand, this test fails before the workflow does."""
    text = _workflow_text()
    assert "ml.cli" in text and "train" in text, (
        "The retrain job must call ``python -m ml.cli train``."
    )
    assert "--manifest-out manifests/current.json" in text, (
        "The manifest path is the API's read target; keep it pinned."
    )


def test_workflow_uploads_artifacts_and_opens_pr():
    """Two delivery channels: ``actions/upload-artifact`` so the model
    binaries are downloadable from the run, and ``create-pull-request``
    so the manifest update lands in a reviewable PR. If you change the
    delivery mechanism (e.g. to a Release upload), update this assert."""
    text = _workflow_text()
    assert "actions/upload-artifact" in text, (
        "Workflow must upload the trained artifacts so they can be deployed."
    )
    assert "create-pull-request" in text, (
        "Workflow must open a PR with the manifest update."
    )


def test_workflow_gates_on_training_database_secret():
    """The job exits cleanly when ``ML_TRAINING_DATABASE_URL`` is unset
    so the cron doesn't fail loudly while operators wire up the secret.
    If you remove that gate, weekly failure emails will start arriving
    on un-configured forks."""
    text = _workflow_text()
    assert "ML_TRAINING_DATABASE_URL" in text, (
        "Workflow must read DATABASE_URL from the ``ML_TRAINING_DATABASE_URL`` secret."
    )
    assert "skipping training" in text.lower(), (
        "The train step must short-circuit cleanly when the secret is unset, "
        "so cron runs on un-configured forks don't fail loudly."
    )
