"""Focused tests for legacy listener mutation request security."""

import unittest
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from thatradiothing.server import WebServer


class ListenerMutationSecurityTests(unittest.TestCase):
    """Verify exact-origin checks without constructing the full service graph."""

    def setUp(self):
        """Create the minimal receiver required by the origin helper."""

        self.server = SimpleNamespace(
            trt=SimpleNamespace(
                url="https://radio.example/",
                cors_allowed_origins=["https://duudey.example"],
            ),
            _normalize_origin=WebServer._normalize_origin,
        )

    def test_cookie_mutations_require_an_allowed_origin(self):
        """Block missing and attacker origins while accepting both first parties."""

        for origin in (None, "https://attacker.example"):
            headers = {"Origin": origin} if origin else {}
            request = make_mocked_request("POST", "/enable", headers=headers)
            with self.assertRaises(web.HTTPForbidden):
                WebServer._require_mutation_origin(self.server, request)

        for origin in ("https://radio.example", "https://duudey.example/"):
            request = make_mocked_request("POST", "/enable", headers={"Origin": origin})
            WebServer._require_mutation_origin(self.server, request)

    def test_bearer_mutations_do_not_depend_on_browser_origin(self):
        """Permit non-cookie API clients authenticated by an explicit bearer JWT."""

        request = make_mocked_request(
            "POST",
            "/enable",
            headers={"Authorization": "Bearer signed-token"},
        )
        WebServer._require_mutation_origin(self.server, request)


if __name__ == "__main__":
    unittest.main()
