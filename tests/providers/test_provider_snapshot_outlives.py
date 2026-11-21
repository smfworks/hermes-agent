"""Regression test for #157742: a forked child that outlives the parent keeps
its provider-plugin snapshot. The parent's normal exit must not delete a
snapshot root while a forked child still imports from it.

The child holds a real OS lock on the parent's owner.lock in its own
descriptor, mirroring the flock the fork inherits. The creating process's exit
hook deletes the root unconditionally today, which orphans the child's
imports.
"""
import os
import types

import pytest


@pytest.fixture
def loaded(tmp_path, monkeypatch):
    """The loader with no root yet; exit hooks are captured, not registered."""
    import providers.plugin_reload as module

    hooks = []
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(module, "_snapshot_root", None)
    monkeypatch.setattr(
        module, "atexit", types.SimpleNamespace(register=lambda fn, *args: hooks.append((fn, args)))
    )
    yield module, hooks


@pytest.mark.platforms("posix")  # os.fork
def test_a_forked_child_that_outlives_the_parent_keeps_the_root(loaded, tmp_path):
    reload, hooks = loaded
    root = reload._new_root()
    ((release, args),) = hooks
    marker = root / "pkg" / "lazy.py"
    marker.parent.mkdir(parents=True)
    marker.write_text("VALUE = 42\n")

    pid = os.fork()
    if pid == 0:  # pragma: no cover - runs in the child
        try:
            # A child that keeps running: it still imports from the root, so it
            # waits past the parent's exit before checking.
            deadline = 5.0
            import time

            while deadline > 0 and root.is_dir():
                time.sleep(0.05)
                deadline -= 0.05
            # The parent exited; the child's lazy import needs the tree.
            ok = marker.is_file()
            code = 0 if ok else 3
        except BaseException:
            code = 4
        os._exit(code)
    # Parent exits normally: its hook must not delete a root a live child
    # reads from. The hook defers while another process holds the lock, and
    # the child, which inherited it open, holds it.
    release(*args)
    _, status = os.waitpid(pid, 0)

    assert os.waitstatus_to_exitcode(status) == 0
    assert root.is_dir()
