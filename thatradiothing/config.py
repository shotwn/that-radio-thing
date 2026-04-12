import os
import os.path
import json

CONFIG_FILE_NAME = 'config.json'
DEFAULTS = {
    'url': 'http://localhost.com',
    'port': 33408,
    'client_id': 'ENTER YOUR CLIENT ID',
    'client_secret': 'ENTER YOUR CLIENT SECRET',
    'masters_list': ['ENTER AT LEAST ONE SPOTIFY USER ID'],
    'scopes': [
        'user-modify-playback-state',
        'user-read-playback-state'
    ],
    'realtime_tolerance_ms': 1000,
    'auth_cookie_name': 'duudey_auth',
    'auth_cookie_domain': None,
    'auth_cookie_secure': True,
    'auth_cookie_samesite': 'Lax',
    'auth_cookie_max_age_seconds': 2592000,
    'auth_jwt_issuer': 'duudey-auth',
    'auth_shared_jwt_secret': 'CHANGE_ME_TO_A_RANDOM_STRING_myMmcQJLoIzhKJoYkDXDTiMZT8RKG1JD',
    'cors_allowed_origins': [],
    'cors_allow_credentials': True,
    'playlists': [{
        'uri': 'ENTER YOUR PLAYLIST URI'
    }]
}

MANDATORY_CONF_FILE_FIELDS = ['client_id', 'client_secret']

CONFIG = dict()
CONFIG.update(DEFAULTS)


def load_config():
    try:
        with open(CONFIG_FILE_NAME, 'r') as read_file:
            config_f = json.load(read_file)
            for field in MANDATORY_CONF_FILE_FIELDS:
                if field not in config_f:
                    print('Missing config file field: ' + field)
                    exit()
            CONFIG.update(config_f)

            env_shared_secret = os.getenv('AUTH_SHARED_JWT_SECRET')
            if env_shared_secret:
                CONFIG['auth_shared_jwt_secret'] = env_shared_secret

            env_cookie_domain = os.getenv('AUTH_COOKIE_DOMAIN')
            if env_cookie_domain is not None:
                CONFIG['auth_cookie_domain'] = env_cookie_domain.strip() or None

            env_cookie_name = os.getenv('AUTH_COOKIE_NAME')
            if env_cookie_name:
                CONFIG['auth_cookie_name'] = env_cookie_name.strip()

            env_issuer = os.getenv('AUTH_JWT_ISSUER')
            if env_issuer:
                CONFIG['auth_jwt_issuer'] = env_issuer.strip()

            env_cookie_secure = os.getenv('AUTH_COOKIE_SECURE')
            if env_cookie_secure is not None:
                CONFIG['auth_cookie_secure'] = env_cookie_secure.strip().lower() in ('1', 'true', 'yes', 'on')

            cookie_samesite = str(CONFIG.get('auth_cookie_samesite', 'Lax')).strip().lower()
            if cookie_samesite == 'none':
                CONFIG['auth_cookie_samesite'] = 'None'
            elif cookie_samesite == 'strict':
                CONFIG['auth_cookie_samesite'] = 'Strict'
            else:
                CONFIG['auth_cookie_samesite'] = 'Lax'

            env_cookie_samesite = os.getenv('AUTH_COOKIE_SAMESITE')
            if env_cookie_samesite is not None:
                env_cookie_samesite = env_cookie_samesite.strip().lower()
                if env_cookie_samesite == 'none':
                    CONFIG['auth_cookie_samesite'] = 'None'
                elif env_cookie_samesite == 'strict':
                    CONFIG['auth_cookie_samesite'] = 'Strict'
                else:
                    CONFIG['auth_cookie_samesite'] = 'Lax'

            env_cookie_max_age = os.getenv('AUTH_TOKEN_TTL_SECONDS')
            if env_cookie_max_age:
                try:
                    CONFIG['auth_cookie_max_age_seconds'] = max(60, int(env_cookie_max_age))
                except ValueError:
                    pass

            cors_allowed_origins = CONFIG.get('cors_allowed_origins', [])
            if isinstance(cors_allowed_origins, str):
                cors_allowed_origins = [cors_allowed_origins]
            if not isinstance(cors_allowed_origins, list):
                cors_allowed_origins = []
            CONFIG['cors_allowed_origins'] = [
                str(origin).strip().rstrip('/')
                for origin in cors_allowed_origins
                if str(origin).strip()
            ]

            env_cors_allowed_origins = os.getenv('CORS_ALLOWED_ORIGINS')
            if env_cors_allowed_origins is not None:
                CONFIG['cors_allowed_origins'] = [
                    origin.strip().rstrip('/')
                    for origin in env_cors_allowed_origins.split(',')
                    if origin.strip()
                ]

            env_cors_allow_credentials = os.getenv('CORS_ALLOW_CREDENTIALS')
            if env_cors_allow_credentials is not None:
                CONFIG['cors_allow_credentials'] = env_cors_allow_credentials.strip().lower() in ('1', 'true', 'yes', 'on')

            print(CONFIG)
    except json.JSONDecodeError:
        print('Couldn\'t load config file. Parse error.')
        exit()


if os.path.isfile('config.json'):
    load_config()
else:
    if __name__ == '__main__':
        print('Config.json doesn\'t exist. Creating a blank one.')
        with open(CONFIG_FILE_NAME, "w") as write_file:
            json.dump(CONFIG, write_file, indent=4, sort_keys=True)
