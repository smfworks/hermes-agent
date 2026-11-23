"""Regression for #159176: when several cron jobs fire in the same minute, the ledger
writers contend for the SQLite write lock. Reads inside a transaction that later writes
are the dangerous shape: a deferred transaction that upgrades mid-flight gets
SQLITE_BUSY_SNAPSHOT immediately when a sibling writer committed since the read — the
busy handler does not cover that failure, so the scheduler sees 'database is locked'.

These tests pin the behavioral contract: a writer that arrives while a sibling holds the
write lock WAITS for it (busy handler) and commits, instead of raising. The
read-then-write bodies take the writer lock up front (BEGIN IMMEDIATE) so the read and
the write land in one atomic turn."""

from __future__ import annotations

import sqlite3
import threading

import pytest


def _point_ledger(monkeypatch, tmp_path):
    from cron import executions

    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    return executions


def _hold_then_release(db_path: str, hold: float) -> None:
    """Hold the SQLite write lock from a sibling connection for `hold` seconds."""
    conn = sqlite3.connect(db_path, timeout=0.2, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        stop = threading.Event()
        timer = threading.Timer(hold, stop.set)
        timer.start()
        stop.wait(timeout=hold + 5)
        timer.cancel()
    finally:
        conn.rollback()
        conn.close()


def test_finish_execution_waits_out_a_sibling_writer(monkeypatch, tmp_path):
    """The finish path must wait for a briefly-held sibling write lock and commit, not
    surface 'database is locked' to the scheduler's bookkeeping."""
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("finish-contention", source="builtin")
    path = str(tmp_path / "cron" / "executions.db")

    blocker = threading.Thread(
        target=_hold_then_release, args=(path, 0.75), daemon=True
    )
    blocker.start()
    # Give the blocker a moment to take the lock before the ledger call.
    ready = threading.Event()
    ready.wait(timeout=0.2)

    finished = executions.finish_execution(record["id"], success=True)
    assert finished is not None
    assert finished["status"] == "completed"
    blocker.join(timeout=5)

    current = executions.get_execution(record["id"])
    assert current is not None
    assert current["status"] == "completed"


def test_receipt_recovery_waits_out_a_sibling_writer(monkeypatch, tmp_path):
    """recover_receipted_execution reads the row then writes it in one transaction. Under
    the #159176 interleaving a deferred upgrade fails fast (busy handler does not retry
    SQLITE_BUSY_SNAPSHOT), so the body takes the writer lock up front and waits like any
    other writer."""
    executions = _point_ledger(monkeypatch, tmp_path)
    # Mint the attempt under a foreign, provably-dead owner so recovery is in scope.
    monkeypatch.setattr(executions, "_PROCESS_ID", "departed-worker")
    monkeypatch.setattr(executions.os, "getpid", lambda: 999999)
    monkeypatch.setattr(executions, "_process_start_time", lambda _pid: 1)
    record = executions.create_execution("receipt-race", source="builtin")
    monkeypatch.undo()
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    path = str(tmp_path / "cron" / "executions.db")

    blocker = threading.Thread(
        target=_hold_then_release, args=(path, 0.75), daemon=True
    )
    blocker.start()
    ready = threading.Event()
    ready.wait(timeout=0.2)

    recovered = executions.recover_receipted_execution(
        record["id"], record["job_id"], success=False, error="late receipt"
    )
    # The attempt is still claimed (no owner fenced it), so recovery settles it.
    assert recovered is not None
    assert recovered["status"] == "failed"
    blocker.join(timeout=5)

    current = executions.get_execution(record["id"])
    assert current is not None
    assert current["status"] == "failed"


def test_incident_upsert_waits_out_a_sibling_writer(monkeypatch, tmp_path):
    from cron import incidents

    monkeypatch.setattr(incidents, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    path = str(tmp_path / "cron" / "executions.db")

    blocker = threading.Thread(
        target=_hold_then_release, args=(path, 0.75), daemon=True
    )
    blocker.start()
    ready = threading.Event()
    ready.wait(timeout=0.2)

    incident_id, _created = incidents.upsert_incident(
        "contended-job", error="boom"
    )
    assert incident_id
    blocker.join(timeout=5)

    with incidents._transaction() as conn:
        row = conn.execute(
            "SELECT state FROM cron_incidents WHERE id=?", (incident_id,)
        ).fetchone()
    assert row is not None
    assert row["state"] == "detected"
