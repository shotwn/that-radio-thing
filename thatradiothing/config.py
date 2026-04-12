import os.path
import os
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
    'auth_cookie_max_age_seconds': 2592000,
    'auth_jwt_issuer': 'duudey-auth',
    'auth_shared_jwt_secret': 'CHANGE_ME_TO_A_RANDOM_STRING_myMmcQJLoIzhKJoYkDXDTiMZT8RKG1JD',
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

            env_cookie_max_age = os.getenv('AUTH_TOKEN_TTL_SECONDS')
            if env_cookie_max_age:
                try:
                    CONFIG['auth_cookie_max_age_seconds'] = max(60, int(env_cookie_max_age))
                except ValueError:
                    pass

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
