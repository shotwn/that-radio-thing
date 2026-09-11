"""Tests for the OAuth callback handler, :meth:`WebServer.auth_return`.

Background for a reader new to the project
------------------------------------------

Logging in is a two-leg round trip:

1. ``GET /auth`` mints a random ``state`` UUID, creates a
   :class:`thatradiothing.user.User` placeholder, appends it to the in-memory
   ``trt.users`` list, and bounces the browser to Spotify.
2. Spotify sends the browser back to ``GET /auth_return?code=…&state=…``. The
   handler finds the placeholder whose ``session_id`` matches ``state``,
   exchanges the code for tokens, and *only then* does the placeholder get a
   populated ``spotify_profile``.

The gap between those two legs is the important part. A ``User`` that reached
leg 1 but never leg 2 -- someone who closed the Spotify consent screen, a bot
that crawled ``/auth``, a health check -- stays in ``trt.users`` forever with
``spotify_profile`` still set to ``None`` (see ``user.User.__init__``). Nothing
ever reaps it.

``auth_return`` then walks that same list to evict older sessions belonging to
the Spotify account that just logged in. These tests pin down that it survives
contact with those never-completed placeholders, because the natural way to
write that loop -- ``prev_user.spotify_profile["id"]`` -- raises ``TypeError``
on ``None`` and turns every subsequent successful login into an HTTP 500.
"""

import asyncio
import unittest
import uuid
from types import SimpleNamespace

from aiohttp.test_utils import make_mocked_request

from thatradiothing.server import WebServer


def _pending_user(session_id):
    """Build a leg-1 placeholder: a session that never finished OAuth.

    This mirrors the real ``User.__init__`` contract -- ``spotify_profile`` is
    ``None`` until ``request_tokens()`` succeeds and loads it.
    """

    return SimpleNamespace(
        session_id=session_id,
        spotify_profile=None,
        auth_code=None,
        request_tokens=None,
    )


def _authenticated_user(session_id, spotify_id):
    """Build a session that will complete OAuth when ``request_tokens`` runs."""

    user = SimpleNamespace(session_id=session_id, spotify_profile=None, auth_code=None)

    async def request_tokens():
        # The real implementation also loads the profile as a side effect,
        # which is exactly what makes the eviction loop below meaningful.
        user.spotify_profile = {"id": spotify_id}
        return True

    user.request_tokens = request_tokens
    return user


class AuthReturnTests(unittest.TestCase):
    """Exercise the callback against realistic ``trt.users`` contents."""

    def setUp(self):
        """Assemble the smallest receiver ``auth_return`` actually touches."""

        self.state = str(uuid.uuid4())
        self.users = []
        self.set_cookie_calls = []

        self.server = SimpleNamespace(
            trt=SimpleNamespace(users=self.users),
            # ``auth_return`` calls this after a successful exchange. Cookie
            # minting is covered elsewhere; here we only record that it ran.
            _set_auth_cookie=lambda response, user: self.set_cookie_calls.append(user),
        )

    def _call(self, request):
        """Invoke the unbound handler against our stand-in receiver."""

        return asyncio.run(WebServer.auth_return(self.server, request))

    def _callback_request(self, cookies=None):
        """Build the request Spotify makes when it hands the browser back.

        ``make_mocked_request`` has no ``cookies`` argument -- aiohttp parses
        ``request.cookies`` out of the ``Cookie`` header, so we serialize the
        jar by hand. Values are sent quoted because that is what aiohttp's
        ``set_cookie`` emits for anything containing a ``/`` (the real service
        responds with ``auth_return_to="/successful_auth"``), and parsing has
        to unquote it again on the way back in.
        """

        jar = {"state": self.state}
        if cookies:
            jar.update(cookies)

        header = "; ".join(f'{name}="{value}"' for name, value in jar.items())

        return make_mocked_request(
            "GET",
            f"/auth_return?code=test-code&state={self.state}",
            headers={"Cookie": header},
        )

    def test_completes_when_a_never_finished_session_is_in_the_list(self):
        """A stale leg-1 placeholder must not break an otherwise valid login.

        This is the production failure: one abandoned ``/auth`` hit poisons
        the list, and every later successful login returns 500.
        """

        self.users.append(_pending_user(str(uuid.uuid4())))
        self.users.append(_authenticated_user(self.state, "spotify-user-1"))

        response = self._call(self._callback_request())

        self.assertEqual(response.status, 302)
        self.assertEqual(response.location, "/successful_auth")
        self.assertEqual(len(self.set_cookie_calls), 1)
        # The unrelated placeholder belongs to a different session and must
        # survive; only same-account sessions get evicted.
        self.assertEqual(len(self.users), 2)

    def test_evicts_earlier_sessions_for_the_same_spotify_account(self):
        """Re-logging in from a second browser retires the first session."""

        previous = _authenticated_user(str(uuid.uuid4()), "spotify-user-1")
        previous.spotify_profile = {"id": "spotify-user-1"}
        current = _authenticated_user(self.state, "spotify-user-1")
        self.users.extend([previous, current])

        response = self._call(self._callback_request())

        self.assertEqual(response.status, 302)
        self.assertEqual(self.users, [current])

    def test_keeps_sessions_belonging_to_other_accounts(self):
        """Two different listeners stay logged in simultaneously."""

        other = _authenticated_user(str(uuid.uuid4()), "spotify-user-2")
        other.spotify_profile = {"id": "spotify-user-2"}
        current = _authenticated_user(self.state, "spotify-user-1")
        self.users.extend([other, current])

        response = self._call(self._callback_request())

        self.assertEqual(response.status, 302)
        self.assertEqual(self.users, [other, current])

    def test_honours_a_safe_return_to_cookie(self):
        """The post-login landing page set at leg 1 is respected."""

        self.users.append(_pending_user(str(uuid.uuid4())))
        self.users.append(_authenticated_user(self.state, "spotify-user-1"))

        response = self._call(self._callback_request({"auth_return_to": "/player"}))

        self.assertEqual(response.status, 302)
        self.assertEqual(response.location, "/player")

    def test_survives_a_token_exchange_that_loaded_no_profile(self):
        """Defence in depth for the other half of the production crash.

        Both sides of the original comparison could be ``None``. ``user.py``
        now refuses to report success without a profile, so ``result`` should
        be falsy long before this loop runs — but the callback must not 500
        even if some future path reintroduces the combination.
        """

        broken = SimpleNamespace(session_id=self.state, spotify_profile=None, auth_code=None)

        async def request_tokens():
            # Deliberately claims success while leaving the profile unset.
            return True

        broken.request_tokens = request_tokens

        other = _authenticated_user(str(uuid.uuid4()), "spotify-user-2")
        other.spotify_profile = {"id": "spotify-user-2"}
        self.users.extend([other, broken])

        response = self._call(self._callback_request())

        self.assertEqual(response.status, 302)
        # No profile means no identity, so nothing may be evicted.
        self.assertEqual(self.users, [other, broken])

    def test_rejects_an_off_site_return_to_cookie(self):
        """A tampered cookie cannot turn the callback into an open redirect."""

        self.users.append(_authenticated_user(self.state, "spotify-user-1"))

        response = self._call(self._callback_request({"auth_return_to": "//evil.example"}))

        self.assertEqual(response.status, 302)
        self.assertEqual(response.location, "/successful_auth")


if __name__ == "__main__":
    unittest.main()
