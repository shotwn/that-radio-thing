"""Test package bootstrap.

``thatradiothing.config`` builds and validates :data:`CONFIG` at import time so
a misconfigured deployment fails at startup rather than halfway through a
request. That is the right behaviour for the service, but it means importing
*any* module in the package requires a complete configuration to be present.

Continuous integration checks out the repository without a ``.env`` file, so
without this bootstrap three test modules fail to import with
``RuntimeError: Required environment variable SPOTIFY_CLIENT_ID is not set``,
and the suite silently runs a subset of its tests. A developer's machine hides
the problem, because ``load_dotenv()`` finds the local (git-ignored) ``.env``.

Populating the required variables here fixes both halves of that, provided
discovery runs with the repository root as its top-level directory::

    python -m unittest discover -s tests -t . -v

The ``-t .`` matters. Without it unittest makes ``tests/`` itself the top-level
directory, imports the modules under bare names such as ``test_admin_api``, and
never imports this package at all. And because ``load_dotenv()`` does
not overwrite variables that already exist, these placeholders also win over a
developer's real ``.env`` -- the suite therefore tests identical configuration
everywhere instead of whatever credentials happen to be lying around.

Never put a real credential here. Nothing in the suite makes a network call,
and every value below is deliberately obvious junk.
"""

from __future__ import annotations

import os

# AUTH_SHARED_JWT_SECRET must clear the 32-byte minimum enforced by
# ``config._parse_secret``; the others only have to be non-empty.
_TEST_ENVIRONMENT = {
    "SPOTIFY_CLIENT_ID": "test-client-id",
    "SPOTIFY_CLIENT_SECRET": "test-client-secret",
    "AUTH_SHARED_JWT_SECRET": "test-shared-secret-that-is-long-enough-for-hs256",
}

for _name, _value in _TEST_ENVIRONMENT.items():
    os.environ.setdefault(_name, _value)
