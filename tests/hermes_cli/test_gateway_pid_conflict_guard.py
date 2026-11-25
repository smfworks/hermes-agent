"""The preflight conflict guard refuses only a PROVED gateway (#159631).

After a reboot Windows recycles the stale ``gateway.pid`` number onto a process the
current user cannot open (``OpenProcess`` → ``ERROR_ACCESS_DENIED``). Every liveness
check then degrades to "record looks valid": the pid "exists", the start time is
unreadable so no conflict is provable, and the unreadable command line falls back to
the persisted record vouching for itself. The preflight in
``_guard_existing_gateway_process_conflict`` took that at face value and
``sys.exit(1)`` — 34 refused desktop boots over five minutes until an unrelated
process released the PID number.

The guard now refuses only when the live process is verifiably a gateway; an identity
that cannot be verified falls through to the authoritative runtime-lock claim inside
``start_gateway``, which still refuses a genuine second instance.
"""

import json
import os

import pytest

from hermes_cli import gateway


_STORE_LAUNCHER_ARGV = ["/install/hermes_cli/main.py", "gateway", "run"]


def _write_records(home) -> None:
    """Persist the boot-wedge shape: a running record whose PID is live-but-unidentifiable."""
    record = {
        "gateway_state": "running",
        "pid": os.getpid(),  # live, but its command line is nothing a gateway runs
        "kind": "hermes-gateway",
        "argv": _STORE_LAUNCHER_ARGV,
        "start_time": None,  # unreadable across the elevation boundary
        "hermes_home": str(home),
    }
    (home / "gateway_state.json").write_text(json.dumps(record), encoding="utf-8")
    (home / "gateway.pid").write_text(json.dumps(dict(record)), encoding="utf-8")


@pytest.fixture
def uninspectable_recycled_pid(tmp_path, monkeypatch):
    """The #159631 process table: cmdline unreadable, start time unreadable, pid alive.

    On Linux this is simulated (``/proc`` is always readable for a same-user process);
    the mocks are exactly what Windows psutil answers for a foreign-session process.
    The records go into the SANDBOXED home the conftest hands every test — the
    unscoped ``get_running_pid()`` the guard calls reads exactly that home.
    """
    from hermes_constants import get_process_hermes_home

    home = get_process_hermes_home()
    _write_records(home)
    monkeypatch.setattr("gateway.status._read_process_cmdline", lambda pid: None)
    monkeypatch.setattr("gateway.status._get_process_start_time", lambda pid: None)
    monkeypatch.setattr(gateway, "_running_under_gateway_supervisor", lambda: False)
    return home


def test_an_unverifiable_pid_does_not_wedge_the_start(uninspectable_recycled_pid):
    gateway._guard_existing_gateway_process_conflict()


def test_a_proved_gateway_still_refuses(uninspectable_recycled_pid, monkeypatch, capsys):
    monkeypatch.setattr(
        "gateway.status._read_process_cmdline",
        lambda pid: " ".join(_STORE_LAUNCHER_ARGV),
    )
    with pytest.raises(SystemExit) as exc:
        gateway._guard_existing_gateway_process_conflict()
    assert exc.value.code == 1
    out = capsys.readouterr()
    assert "already running (PID" in out.err + out.out


def test_a_foreign_cmdline_is_not_proof_either(uninspectable_recycled_pid):
    """A readable command line that is not a gateway's is the same non-proof."""
    from gateway import status

    status._read_process_cmdline = lambda pid: "chrome --flag"
    gateway._guard_existing_gateway_process_conflict()
