"""Tests for :meth:`thatradiothing.user.User.request_tokens`.

``request_tokens`` is the second leg of the Spotify OAuth handshake. It does
two distinct things that the rest of the service treats as one unit:

1. Exchanges the authorization code for an access/refresh token pair.
2. Calls ``users_profile()``, which populates ``spotify_profile``.

Step 2 matters as much as step 1. ``spotify_profile`` is the only thing that
identifies *which* Spotify account a session belongs to, and
``server.WebServer._set_auth_cookie`` refuses to mint a JWT without it. A
``True`` return with no profile is therefore not a partial success, it is a
failed login that lies about it -- so these tests pin the return value to
"both halves worked".
"""

import asyncio
import unittest
from types import SimpleNamespace

from thatradiothing.user import User


class _FakeResponse:
    """Minimal stand-in for an ``aiohttp`` response used as a context manager."""

    def __init__(self, status, payload=None, text=""):
        self.status = status
        self._payload = payload or {}
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, content_type=None):
        return self._payload

    async def text(self):
        return self._text


class _FakeSession:
    """Return a canned response for the token endpoint, recording the call."""

    def __init__(self, response):
        self._response = response
        self.posts = []

    def post(self, url, data=None):
        self.posts.append((url, data))
        return self._response


def _token_payload():
    """A well-formed Spotify token-exchange response body."""

    return {
        "access_token": "test-access-token",
        "token_type": "Bearer",
        "scope": "user-read-playback-state",
        "expires_in": 3600,
        "refresh_token": "test-refresh-token",
    }


class RequestTokensTests(unittest.TestCase):
    """Verify the success/failure contract of the token exchange."""

    def _user(self, response, profile_result):
        """Build a ``User`` whose network and profile calls are stubbed out."""

        user = User(
            trt=SimpleNamespace(masters_list=[]),
            session_id="test-session",
            redirect_uri="https://radio.example/auth_return",
            client_id="test-client-id",
            client_secret="test-client-secret",
        )
        user.auth_code = "test-auth-code"

        session = _FakeSession(response)

        async def aiohttp_session():
            return session

        async def users_profile():
            # Mirrors the real method: it sets the attribute only on success.
            if profile_result:
                user.spotify_profile = profile_result
            return profile_result

        user.aiohttp_session = aiohttp_session
        user.users_profile = users_profile
        return user

    def test_returns_true_when_tokens_and_profile_both_load(self):
        """The ordinary happy path leaves the session fully identified."""

        user = self._user(_FakeResponse(200, _token_payload()), {"id": "spotify-user-1"})

        self.assertIs(asyncio.run(user.request_tokens()), True)
        self.assertEqual(user.spotify_profile, {"id": "spotify-user-1"})
        self.assertEqual(user.access_token, "test-access-token")

    def test_returns_false_when_the_profile_cannot_be_loaded(self):
        """A rejected ``/v1/me`` must fail the login, not fake a success.

        Spotify can hand back perfectly valid tokens and still refuse the
        profile call -- a 403 while the app sits in development mode is the
        common case. Returning ``True`` here left ``spotify_profile`` as
        ``None`` and pushed an unidentifiable session into ``trt.users``.
        """

        user = self._user(_FakeResponse(200, _token_payload()), False)

        self.assertIs(asyncio.run(user.request_tokens()), False)
        self.assertIsNone(user.spotify_profile)

    def test_returns_false_when_the_token_endpoint_rejects_the_code(self):
        """A non-200 from the token endpoint short-circuits before the profile."""

        user = self._user(_FakeResponse(400, text="invalid_grant"), {"id": "spotify-user-1"})

        self.assertIs(asyncio.run(user.request_tokens()), False)
        self.assertIsNone(user.spotify_profile)

    def test_returns_false_when_the_token_body_carries_an_error(self):
        """Spotify can answer 200 with an ``error`` field; treat it as failure."""

        user = self._user(
            _FakeResponse(200, {"error": "invalid_grant"}),
            {"id": "spotify-user-1"},
        )

        self.assertIs(asyncio.run(user.request_tokens()), False)
        self.assertIsNone(user.spotify_profile)


if __name__ == "__main__":
    unittest.main()
