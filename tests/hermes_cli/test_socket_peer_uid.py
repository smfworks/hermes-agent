"""SO_PEERCRED uid must compare equal to os.getuid() above signed 32-bit."""
import os
import socket
import struct

import pytest

from hermes_cli.gateway_runtime_discovery import socket_peer_uid

pytestmark = pytest.mark.platforms("linux")  # SO_PEERCRED ucred layout


class _CredSocket:
    def __init__(self, raw: bytes):
        self.raw = raw

    def getsockopt(self, level, opt, buflen):
        assert (level, opt, buflen) == (socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        return self.raw


def test_socket_peer_uid_matches_getuid_when_uid_exceeds_signed_32bit(monkeypatch):
    """A uid_t at or above 2**31 must not wrap. Callers treat a mismatch as unsafe_control_peer."""
    uid = 3093752771
    assert uid >= 2**31
    peer = _CredSocket(struct.pack("iII", 4242, uid, 1000))
    monkeypatch.setattr(os, "getuid", lambda: uid)

    reported = socket_peer_uid(peer)

    assert reported == os.getuid()


@pytest.mark.parametrize("uid", [0, 1, 2**31 - 1, 2**31, 2**32 - 1, 3093752771])
def test_socket_peer_uid_roundtrips_uid_t(monkeypatch, uid):
    monkeypatch.setattr(os, "getuid", lambda: uid)
    reported = socket_peer_uid(_CredSocket(struct.pack("iII", 42, uid, 7)))

    assert reported == uid
    assert reported == os.getuid()


def test_a_different_high_uid_is_still_not_this_user(monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 2**31)
    reported = socket_peer_uid(_CredSocket(struct.pack("iII", 1, 2**31 + 1, 1)))

    assert reported == 2**31 + 1
    assert reported != os.getuid()


def test_socket_peer_uid_reports_the_peer_not_the_caller(monkeypatch):
    """A wrapped read must not be papered over by returning os.getuid()."""
    peer_uid = 3093752771
    monkeypatch.setattr(os, "getuid", lambda: 1000)
    reported = socket_peer_uid(_CredSocket(struct.pack("iII", 7, peer_uid, 9)))

    assert reported == peer_uid
    assert reported != os.getuid()


def test_socket_peer_uid_still_matches_an_ordinary_uid(monkeypatch):
    uid = 1000
    monkeypatch.setattr(os, "getuid", lambda: uid)
    reported = socket_peer_uid(_CredSocket(struct.pack("iII", 1, uid, 1)))

    assert reported == os.getuid()


def test_socket_peer_uid_matches_the_kernel_on_a_real_socketpair():
    ours, theirs = socket.socketpair(socket.AF_UNIX)
    try:
        assert socket_peer_uid(ours) == os.getuid()
        assert socket_peer_uid(theirs) == os.getuid()
    finally:
        ours.close()
        theirs.close()
