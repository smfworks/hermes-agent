"""An explicitly-set EMPTY per-job toolset allowlist must mean 'nothing allowed' end to end.

The create/update normalizers turned ``enabled_toolsets=[]`` into null, and the dispatch
resolver treated a stored ``[]`` as absent (falsy), so a locked-down job silently widened
back to the platform default — every toolset (#82010). Absent/null keeps meaning
'no per-job restriction'."""

from __future__ import annotations

import pytest


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    """Isolate HERMES_HOME for each test so jobs don't leak between tests."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "scripts").mkdir()
    (home / "cron").mkdir()

    monkeypatch.setenv("HERMES_HOME", str(home))

    import importlib

    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.jobs
    importlib.reload(cron.jobs)
    import cron.scheduler
    importlib.reload(cron.scheduler)
    return home


def test_create_persists_explicit_empty_allowlist_and_survives_reload(hermes_env):
    import importlib

    from cron.jobs import create_job

    job = create_job(prompt="daily digest", schedule="every 1h", enabled_toolsets=[])
    assert job["enabled_toolsets"] == []

    # A fresh process (module reload) must read the restriction back from jobs.json as [],
    # not null — null would read as 'no restriction' and widen the job.
    import cron.jobs
    importlib.reload(cron.jobs)
    reloaded = cron.jobs.get_job(job["id"])
    assert reloaded is not None and reloaded["enabled_toolsets"] == []


def test_update_path_preserves_explicit_empty_allowlist(hermes_env):
    from cron.jobs import create_job, update_job

    job = create_job(prompt="daily digest", schedule="every 1h")
    assert job["enabled_toolsets"] is None  # absent = no per-job restriction

    updated = update_job(job["id"], {"enabled_toolsets": []})
    assert updated is not None and updated["enabled_toolsets"] == []


def test_scheduler_resolves_explicit_empty_as_zero_toolsets(hermes_env):
    from cron.scheduler import _resolve_cron_enabled_toolsets

    # [] = zero toolsets, fail closed — not a fall-through to the platform default.
    assert _resolve_cron_enabled_toolsets({"enabled_toolsets": []}, {}) == []
    # Absent stays 'no per-job restriction': the platform default selection still applies.
    assert _resolve_cron_enabled_toolsets({}, {})


def test_cronjob_tool_roundtrip_preserves_explicit_empty(hermes_env):
    """The reporter's exact path: cronjob create with enabled_toolsets=[] from a session."""
    import json

    from cron.jobs import get_job
    from tools.cronjob_tools import cronjob

    result = json.loads(cronjob(
        action="create", prompt="daily digest", schedule="every 1h", enabled_toolsets=[]))
    assert result["success"] is True, result

    job = get_job(result["job_id"])
    assert job is not None and job["enabled_toolsets"] == []


def test_dashboard_normalize_preserves_explicit_empty_allowlist(tmp_path):
    from hermes_cli.web_routers.cron import _normalize_dashboard_cron_updates

    out = _normalize_dashboard_cron_updates(
        {"prompt": "format this better", "enabled_toolsets": []}, tmp_path)
    assert out["enabled_toolsets"] == []
    assert out["prompt"] == "format this better"

    named = _normalize_dashboard_cron_updates({"enabled_toolsets": ["terminal"]}, tmp_path)
    assert named["enabled_toolsets"] == ["terminal"]


def test_dashboard_still_collapses_empty_context_from(tmp_path):
    from hermes_cli.web_routers.cron import _normalize_dashboard_cron_updates

    out = _normalize_dashboard_cron_updates({"context_from": []}, tmp_path)
    assert out["context_from"] is None


def test_update_job_none_toolsets_does_not_widen_empty_allowlist(hermes_env):
    from cron.jobs import create_job, update_job

    job = create_job(prompt="format this", schedule="every 1h", enabled_toolsets=[])
    updated = update_job(job["id"], {"prompt": "format this better", "enabled_toolsets": None})
    assert updated is not None
    assert updated["enabled_toolsets"] == []
    assert updated["prompt"] == "format this better"


def test_dashboard_prompt_edit_keeps_zero_tool_allowlist(hermes_env, tmp_path):
    """The reported path: a prompt-only dashboard save used to turn [] into null."""
    from cron.jobs import create_job, update_job
    from hermes_cli.web_routers.cron import _normalize_dashboard_cron_updates

    job = create_job(prompt="format this", schedule="every 1h", enabled_toolsets=[])
    updates = _normalize_dashboard_cron_updates(
        {"prompt": "format this better", "enabled_toolsets": []}, tmp_path)
    updated = update_job(job["id"], updates)
    assert updated is not None
    assert updated["enabled_toolsets"] == []
    assert updated["prompt"] == "format this better"
