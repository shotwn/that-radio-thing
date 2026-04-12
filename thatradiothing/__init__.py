import asyncio
import thatradiothing.server
import thatradiothing.master
import thatradiothing.autodj
from thatradiothing.config import CONFIG


class ThatRadioThing:
    def __init__(self):
        self.url = CONFIG['url']
        self.port = CONFIG['port']
        self.client_id = CONFIG['client_id']
        self.client_secret = CONFIG['client_secret']
        self.scopes = CONFIG['scopes']
        self.realtime_tolerance_ms = CONFIG['realtime_tolerance_ms']
        self.masters_list = CONFIG['masters_list']
        self.auth_cookie_name = CONFIG['auth_cookie_name']
        self.auth_cookie_domain = CONFIG['auth_cookie_domain']
        self.auth_cookie_secure = bool(CONFIG['auth_cookie_secure'])
        self.auth_cookie_max_age_seconds = int(CONFIG['auth_cookie_max_age_seconds'])
        self.auth_jwt_issuer = CONFIG['auth_jwt_issuer']
        self.auth_shared_jwt_secret = CONFIG['auth_shared_jwt_secret']

        self.users = []

        self.web_server = thatradiothing.server.WebServer(self)
        self.web_server_task = None
        self.master = thatradiothing.master.Master(self)
        self.master_task = None
        self.autodj = thatradiothing.autodj.AutoDJ(self, 'AUTODJ', 'AUTODJ', self.client_id, self.client_secret, playlists=CONFIG['playlists'])

    def run(self):
        asyncio.run(self._run())

    async def _run(self):
        self.web_server_task = asyncio.create_task(self.web_server.run())
        self.web_server_task.add_done_callback(self.aio_exception_handler)
        self.master_task = asyncio.create_task(self.master.beat())
        self.master_task.add_done_callback(self.aio_exception_handler)

        await asyncio.Event().wait()

    def aio_exception_handler(self, future):
        if future.exception():
            future.result()

    find_user_allowed_keys = [
        'session_id',
    ]

    async def find_users(self, **kwargs):
        if not kwargs:
            raise ValueError()

        for kw in kwargs:
            if kw not in self.find_user_allowed_keys:
                raise KeyError()

        found_users = []
        for user in self.users:
            match = False
            for key, value in kwargs.items():
                if str(getattr(user, str(key))) == str(value):
                    continue
                break
            else:
                match = True

            if match:
                found_users.append(user)

        return found_users

    async def find_user(self, **kwargs):
        users = await self.find_users(**kwargs)
        if not users:
            return None

        if len(users) == 1:
            return users[0]
