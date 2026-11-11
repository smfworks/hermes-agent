"""A wedged file tool must hold its environment against the idle reaper.

#152557: a ``write_file`` blocked ~458 s and the idle reaper tore the terminal
environment down mid-call — the GW-60 guard (#144597) covered terminal and
execute_code but not the file tools, on the assumption their calls are short.
The file tools now take the same refcounted hold, so a reaper sweep that fires
inside a wedged file op sees the environment busy instead of stale.
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from tools import file_tools, terminal_tool as tt
from tools.terminal_tool_lifecycle import _cleanup_inactive_envs


class _WedgingEnv:
    """An env whose file write never returns: one reaper sweep fires inside it."""

    def __init__(self):
        self.cleaned = False
        self.cleaned_mid_call = None

    def cleanup(self):
        self.cleaned = True

    def write_file(self, path, content):
        _sweep_after_long_call()
        self.cleaned_mid_call = self.cleaned
        from tools.file_operations_common import WriteResult
        return WriteResult(bytes_written=len(content))

    def read_file(self, path, offset=1, limit=1000):
        from tools.file_operations_common import ReadResult
        return ReadResult(content="x\n", total_lines=1)

    def read_wedging(self, path, offset=1, limit=1000):
        """read_file but wedging: sweep fires inside the read."""
        _sweep_after_long_call()
        self.cleaned_mid_call = self.cleaned
        return self.read_file(path, offset, limit)


def _sweep_after_long_call():
    for key in list(tt._last_activity):
        tt._last_activity[key] = time.time() - 600
    _cleanup_inactive_envs(lifetime_seconds=300)


@pytest.fixture()
def env(monkeypatch):
    """A shared env cached under the collapsed key ``default`` with only the
    sweeps this test fires; ShellFileOperations stubbed to the wedging env."""
    monkeypatch.setattr(tt, "_active_environments", {})
    monkeypatch.setattr(tt, "_last_activity", {})
    monkeypatch.setattr(tt, "_creation_locks", {})
    monkeypatch.setattr(tt, "_calls_in_flight", {})
    monkeypatch.setattr(tt, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(file_tools, "_file_ops_cache", {})
    wedging = _WedgingEnv()
    tt._active_environments["default"] = wedging
    tt._last_activity["default"] = time.time()
    ops = MagicMock()
    ops.env = wedging
    ops.write_file = wedging.write_file
    ops.read_file = wedging.read_file
    return wedging


def ops_stub(env, wedging_read=False):
    ops = MagicMock()
    ops.env = env
    ops.write_file = env.write_file
    ops.read_file = env.read_wedging if wedging_read else env.read_file
    return ops


def test_a_wedged_write_file_keeps_its_environment_past_the_lifetime(env):
    with patch.object(file_tools, "_get_file_ops", return_value=ops_stub(env)):
        result = json.loads(file_tools.write_file_tool("/tmp/out.txt", "data"))

    assert "error" not in result, result
    assert env.cleaned_mid_call is False
    assert tt._active_environments["default"] is env
    # Idle again once the call returns: the countdown restarts from the call's end.
    _sweep_after_long_call()
    assert env.cleaned


def test_a_wedged_read_file_keeps_its_environment_past_the_lifetime(env, monkeypatch):
    monkeypatch.setattr(file_tools, "_check_not_found_cache", lambda *a, **k: None)
    with patch.object(file_tools, "_get_file_ops", return_value=ops_stub(env, wedging_read=True)):
        json.loads(file_tools.read_file_tool("/tmp/out.txt"))

    assert env.cleaned_mid_call is False
    assert tt._active_environments["default"] is env
