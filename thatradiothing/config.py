import os
import json

try:
    from dotenv import load_dotenv
    # Load .env from the working directory (or parents) on import so the
    # values below are available without the operator having to source it.
    load_dotenv()
except ImportError:
    pass


def _split_csv(value, default=None):
    if value is None:
        return list(default) if default is not None else []
    return [item.strip() for item in str(value).split(',') if item.strip()]


def _parse_bool(value, default):
    if value is None:
        return default
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def _parse_samesite(value, default='Lax'):
    normalized = (value or default).strip().lower()
    if normalized == 'none':
        return 'None'
    if normalized == 'strict':
        return 'Strict'
    return 'Lax'


def _parse_json(value, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _parse_int(value, default, minimum=None):
    if value is None or str(value).strip() == '':
        return default
    try:
        parsed = int(str(value).strip())
    except ValueError:
        return default
    if minimum is not None and parsed < minimum:
        return minimum
    return parsed


def _require(name):
    value = os.getenv(name)
    if not value or not value.strip():
        raise RuntimeError(
            f'Required environment variable {name} is not set. '
            f'Copy .env.example to .env and fill it in, or export the value in your shell.'
        )
    return value.strip()


CONFIG = {
    'url': os.getenv('TRT_URL', 'http://localhost:33408/'),
    'port': _parse_int(os.getenv('TRT_PORT'), 33408, minimum=1),
    'client_id': _require('SPOTIFY_CLIENT_ID'),
    'client_secret': _require('SPOTIFY_CLIENT_SECRET'),
    'masters_list': _split_csv(os.getenv('TRT_MASTERS_LIST')),
    'scopes': _split_csv(
        os.getenv('TRT_SCOPES'),
        default=['user-modify-playback-state', 'user-read-playback-state'],
    ),
    'realtime_tolerance_ms': _parse_int(os.getenv('TRT_REALTIME_TOLERANCE_MS'), 1000, minimum=0),
    'playlists': _parse_json(os.getenv('TRT_PLAYLISTS'), []),
    'auth_cookie_name': os.getenv('AUTH_COOKIE_NAME', 'duudey_auth'),
    'auth_cookie_domain': (os.getenv('AUTH_COOKIE_DOMAIN') or '').strip() or None,
    'auth_cookie_secure': _parse_bool(os.getenv('AUTH_COOKIE_SECURE'), True),
    'auth_cookie_samesite': _parse_samesite(os.getenv('AUTH_COOKIE_SAMESITE')),
    'auth_cookie_max_age_seconds': _parse_int(os.getenv('AUTH_TOKEN_TTL_SECONDS'), 2592000, minimum=60),
    'auth_jwt_issuer': os.getenv('AUTH_JWT_ISSUER', 'duudey-auth'),
    'auth_shared_jwt_secret': _require('AUTH_SHARED_JWT_SECRET'),
    'cors_allowed_origins': [
        origin.rstrip('/')
        for origin in _split_csv(os.getenv('CORS_ALLOWED_ORIGINS'))
    ],
    'cors_allow_credentials': _parse_bool(os.getenv('CORS_ALLOW_CREDENTIALS'), True),
}
