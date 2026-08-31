"""Tests for the Socket.IO status fan-out.

The regression these guard against: an administrator connecting from
duudey.com stopped receiving the listener ``status`` event, because the
gateway emitted ``admin_status`` *instead of* it. That event is the only
source of ``can_be_master``, ``profile``, ``devices`` and ``message`` for the
site player, so being a radio administrator silently removed the operator's
"Go Master" button while the radio's own player -- which reads REST endpoints
rather than the socket -- kept showing it.

Administrator is an additional role, not a replacement one, so an admin socket
must receive both streams.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from thatradiothing.sio import ADMIN_STATUS_EVENT, STATUS_EVENT, SocketIOGateway


class FakeSession:
    """Async context manager returning one mutable Socket.IO session dict."""

    def __init__(self, data):
        """Store the session mapping handed back to the gateway."""

        self.data = data

    async def __aenter__(self):
        """Return the underlying session mapping."""

        return self.data

    async def __aexit__(self, *exc_info):
        """Leave the session untouched on exit."""

        return False


class FakeSio:
    """Record emitted events instead of writing to a socket."""

    def __init__(self, session_data):
        """Seed the single session this fake serves."""

        self._session_data = session_data
        self.emitted: list[tuple[str, dict]] = []

    def session(self, sid):
        """Return the seeded session regardless of *sid*."""

        del sid
        return FakeSession(self._session_data)

    async def emit(self, event, payload, to=None):
        """Capture one emitted event."""

        del to
        self.emitted.append((event, payload))


def build_gateway(*, is_admin: bool) -> SocketIOGateway:
    """Build a gateway with its constructor bypassed and fakes attached."""

    gateway = SocketIOGateway.__new__(SocketIOGateway)
    gateway._last_payloads = {"sid": None}
    gateway._last_emit_monotonic = {}
    gateway._last_admin_payloads = {}
    gateway._last_admin_emit_monotonic = {}
    gateway.sio = FakeSio({"session_id": "session-1", "is_admin": is_admin})

    user = SimpleNamespace(auth_headers=_always_authorized)
    gateway.trt = SimpleNamespace(find_user=_finder(user))
    gateway.web_server = SimpleNamespace(
        build_status_payload=_listener_payload,
        admin_api=SimpleNamespace(status_payload=_admin_payload),
    )
    return gateway


async def _always_authorized():
    """Report the Spotify token as usable."""

    return True


def _finder(user):
    """Return a ``find_user`` coroutine that always resolves to *user*."""

    async def find_user(session_id):
        del session_id
        return user

    return find_user


async def _listener_payload(user):
    """Return a minimal listener payload carrying the master capability."""

    del user
    return {"can_be_master": True, "is_master": False, "profile": {"id": "operator"}}


async def _admin_payload():
    """Return a minimal control-room payload."""

    return {"schedule": {"degraded": False}}


class StatusFanOutTests(unittest.TestCase):
    """Both roles must keep receiving the listener stream."""

    def test_listener_receives_only_the_status_event(self):
        """A non-admin socket gets ``status`` and never ``admin_status``."""

        gateway = build_gateway(is_admin=False)
        asyncio.run(gateway._emit_for_sid("sid"))
        events = [event for event, _ in gateway.sio.emitted]
        self.assertEqual(events, [STATUS_EVENT])

    def test_admin_receives_the_listener_stream_as_well(self):
        """An admin socket gets ``status`` too, not just ``admin_status``."""

        gateway = build_gateway(is_admin=True)
        asyncio.run(gateway._emit_for_sid("sid"))
        events = [event for event, _ in gateway.sio.emitted]
        self.assertIn(STATUS_EVENT, events)
        self.assertIn(ADMIN_STATUS_EVENT, events)

        # The listener payload is what carries the master capability; losing
        # it is precisely the bug under test.
        listener = next(p for event, p in gateway.sio.emitted if event == STATUS_EVENT)
        self.assertTrue(listener["can_be_master"])

    def test_admin_streams_are_diffed_independently(self):
        """An unchanged admin payload must not suppress a changed listener one."""

        gateway = build_gateway(is_admin=True)
        asyncio.run(gateway._emit_for_sid("sid"))
        gateway.sio.emitted.clear()

        # Listener state changes; the control-room payload does not.
        async def changed_listener(user):
            del user
            return {"can_be_master": True, "is_master": True, "profile": {"id": "operator"}}

        gateway.web_server.build_status_payload = changed_listener
        asyncio.run(gateway._emit_for_sid("sid"))
        events = [event for event, _ in gateway.sio.emitted]
        self.assertIn(STATUS_EVENT, events)
        self.assertNotIn(ADMIN_STATUS_EVENT, events)


if __name__ == "__main__":
    unittest.main()
