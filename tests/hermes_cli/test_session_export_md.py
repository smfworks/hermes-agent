from pathlib import Path

import pytest
import re

from hermes_cli.session_export_md import (
    append_manifest_entry,
    render_session_markdown,
    safe_session_filename,
    verify_export_file,
    write_session_markdown,
)


def _session(**overrides):
    data = {
        "id": "20260706_123456_abcd1234",
        "title": "Export Test",
        "source": "telegram",
        "model": "gpt-5.5",
        "billing_provider": "openai-codex",
        "cwd": "/tmp/project",
        "started_at": 1783331696.0,
        "last_active": 1783331705.0,
        "ended_at": 1783331710.0,
        "message_count": 3,
        "tool_call_count": 1,
        "archived": 0,
        "messages": [
            {"role": "user", "content": "Hello", "created_at": 1783331697.0},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "terminal", "arguments": "{\"command\": \"pwd\"}"}}
                ],
                "created_at": 1783331698.0,
            },
            {"role": "tool", "name": "terminal", "content": "output", "created_at": 1783331699.0},
        ],
    }
    data.update(overrides)
    return data




def test_safe_session_filename_is_deterministic_and_path_safe():
    filename = safe_session_filename(
        _session(id="20260706_123456_abcd1234", title="Bad / title: * ?"), fmt="qmd"
    )

    assert filename.startswith("20260706_123456_abcd1234-")
    assert filename.endswith(".qmd")
    assert "/" not in filename
    assert ":" not in filename
    assert "*" not in filename
    assert "?" not in filename






def test_verify_export_file_checks_count_and_sha(tmp_path):
    session = _session()
    path = write_session_markdown(session, tmp_path)

    ok, reason = verify_export_file(path, session)
    assert ok is True
    assert reason == "ok"

    path.write_text(path.read_text(encoding="utf-8").replace("Hello", "Tampered"), encoding="utf-8")
    ok, reason = verify_export_file(path, session)
    assert ok is False
    assert "sha256" in reason




def _self_referential_session(**overrides):
    data = {
        "id": "20260101_000000_selfref",
        "title": "Self-referential",
        "source": "telegram",
        "model": "m",
        "billing_provider": "p",
        "cwd": "/tmp",
        "message_count": 2,
        "tool_call_count": 0,
        "archived": 0,
        "messages": [
            {
                "role": "user",
                "content": (
                    "the exporter source defines:\n"
                    '_SHA_PLACEHOLDER = "__SHA256_PLACEHOLDER__"'
                ),
                "created_at": 1783331697.0,
            },
            {
                "role": "assistant",
                "content": (
                    "the archived export said:\n"
                    "- SHA256 of exported body: "
                    "`bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb`\n"
                    "- Session id: `old`"
                ),
                "created_at": 1783331698.0,
            },
        ],
    }
    data.update(overrides)
    return data


def test_render_keeps_quoted_placeholder_and_footer_intact():
    out = render_session_markdown(_self_referential_session())

    # The quoted source line and the quoted stale footer must survive byte-for-byte.
    assert '"__SHA256_PLACEHOLDER__"' in out
    assert "`bbbbbbbb...bbbb`" in out or "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" in out

    # Exactly one real SHA line — the generated footer — inside the verification block.
    # The quoted stale footer in the body keeps its original (different) digest.
    sha_lines = [
        line for line in out.splitlines() if "- SHA256 of exported body:" in line
    ]
    digest_lines = [l for l in sha_lines if _SHA_LINE_DIGEST_RE.match(l)]
    assert len(digest_lines) == 2
    digests = {
        m.group(1) for m in (re.search(r"`([0-9a-f]{64})`", l) for l in digest_lines) if m
    }
    assert len(digests) == 2  # the quoted stale digest differs from the real footer


_SHA_LINE_DIGEST_RE = re.compile(r"- SHA256 of exported body: `[0-9a-f]{64}`")


def test_verify_accepts_self_referential_export(tmp_path):
    session = _self_referential_session()
    path = write_session_markdown(session, tmp_path)

    ok, reason = verify_export_file(path, session)
    assert ok is True
    assert reason == "ok"


def test_verify_still_rejects_tampered_self_referential_export(tmp_path):
    session = _self_referential_session()
    path = write_session_markdown(session, tmp_path)
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("__SHA256_PLACEHOLDER__", "__SHA256_PLACEHOLDERS__"),
        encoding="utf-8",
    )

    ok, reason = verify_export_file(path, session)
    assert ok is False
    assert "sha256" in reason
