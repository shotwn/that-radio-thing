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
    'realtime_tolerance_ms': 1000
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
            print(CONFIG)
    except json.JSONDecodeError:
        print('Couldn\'t load config file. Parse error.')
        exit()

if os.path.isfile('config.json'):
    load_config()
else:
    if __name__ == '__main__':
        print ('Config.json doesn\'t exist. Creating a blank one.')
        with open(CONFIG_FILE_NAME, "w") as write_file:
            json.dump(CONFIG, write_file, indent=4, sort_keys=True)