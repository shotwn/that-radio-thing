import time
import aiohttp
import random
import thatradiothing.user
import thatradiothing.logger as logger


class AutoDJ(thatradiothing.user.User):
    def __init__(self, *args, **kwargs):
        super().__init__(*args)
        self.enabled = False
        self.playlists = kwargs['playlists']

        self.selected_playlist = None
        self.now_playing = {
            'track_started_at': time.time(),
            'track': None
        }

        self.spotify_profile = {
            'display_name': 'AutoDJ',
            'external_urls': {
                'spotify': '#'
            }
        }

    async def populate(self):
        await self.request_tokens()
        self.trt.users.append(self)

        await self.select_playlist(self.playlists[0])

    async def select_playlist(self, playlist):
        playlist['data'] = await self.get_playlist(playlist['uri'])
        self.selected_playlist = self.playlists[0]

    async def request_tokens(self):
        payload = {
            "grant_type": 'client_credentials',
        }
        session = await self.aiohttp_session()
        tokens_url = 'https://accounts.spotify.com/api/token'
        auth = aiohttp.BasicAuth(self.client_id, self.client_secret)
        async with session.post(tokens_url, data=payload, auth=auth) as response:
            if response.status != 200:
                logger.error('ERROR AUTODJ CREDENTIALS')
                logger.error(await response.text())

            data = await response.json(content_type=None)

            if data.get('error'):
                logger.error('ERROR AUTODJ CREDENTIALS AFTER OK STATUS CODE')
                logger.error(data)

            self.access_token = data["access_token"]
            self.expires_in = int(data["expires_in"])
            self.refresh_tokens_after = time.time() + self.expires_in - 60
            # self.scope = data["scope"]
            self.token_type = data["token_type"]
            return True

    async def refresh_tokens(self):
        return await self.request_tokens()

    async def get_random_track(self):
        rand = random.choice(self.selected_playlist['data']['tracks']['items'])
        return rand['track']

    async def populate_track(self, has_been_playing_for_ms=0):
        try:
            track = self.now_playing['track']['next_track']
        except (KeyError, TypeError):
            logger.debug('AUTODJ: First run, getting next track randomly.')
            track = await self.get_random_track()

        next_track = await self.get_random_track()

        self.now_playing['track'] = {}
        self.now_playing['track']['item'] = track
        self.now_playing['track']['next_track'] = next_track
        self.now_playing['track']['progress_ms'] = has_been_playing_for_ms  # Milliseconds
        self.now_playing['track']['is_playing'] = True
        self.now_playing['playback_started_at'] = time.time() - has_been_playing_for_ms / 1000  # Seconds
        logger.debug(f"AUTODJ: Cue in -> {self.now_playing['track']['item']['name']}")
        # FOR DEBUG 4 min inside
        # self.now_playing['playback_started_at'] = time.time() - (has_been_playing_for_ms / 1000 + 4 * 60)
        # self.now_playing['track']['progress_ms'] = has_been_playing_for_ms + 4 * 60

    async def currently_playing(self, raise_exception=False, get_next_from_context=False):
        if not self.now_playing['track']:
            await self.populate_track()
        else:
            self.now_playing['track']['progress_ms'] = int((time.time() - self.now_playing['playback_started_at']) * 1000)

            overshoot = self.now_playing['track']['progress_ms'] - self.now_playing['track']['item']['duration_ms']
            # logger.debug(f"overshoot: {overshoot} or {int(overshoot/1000)} seconds")
            if overshoot > 0:
                await self.populate_track(has_been_playing_for_ms=overshoot)
                # return None  # EXPERIMENT: Simulate the pause on song to song pass ?

        # print('---')
        # print(json.dumps(self.now_playing))
        # print(self.now_playing['track']['progress_ms'])
        return self.now_playing['track']
        """
        example_track = {
            "timestamp": 1584284634340,
            "context": {
                "external_urls": {
                    "spotify": "https://open.spotify.com/artist/3oBGGy1VOs6IHOa1ZdUx2f"
                },
                "href": "https://api.spotify.com/v1/artists/3oBGGy1VOs6IHOa1ZdUx2f",
                "type": "artist",
                "uri": "spotify:artist:3oBGGy1VOs6IHOa1ZdUx2f"
            },
            "progress_ms": 232759,
            "item": {
                "album": {
                    "album_type": "album",
                    "artists": [
                        {
                            "external_urls": {
                                "spotify": "https://open.spotify.com/artist/3oBGGy1VOs6IHOa1ZdUx2f"
                            },
                            "href": "https://api.spotify.com/v1/artists/3oBGGy1VOs6IHOa1ZdUx2f",
                            "id": "3oBGGy1VOs6IHOa1ZdUx2f",
                            "name": "Sons Of Apollo",
                            "type": "artist",
                            "uri": "spotify:artist:3oBGGy1VOs6IHOa1ZdUx2f"
                        }
                    ],
                    "available_markets": [
                        "AD",
                        "AE",
                        "AR",
                        "AT",
                        "AU",
                        "BE",
                        "BG",
                        "BH",
                        "BO",
                        "BR",
                        "CA",
                        "CH",
                        "CL",
                        "CO",
                        "CR",
                        "CY",
                        "CZ",
                        "DE",
                        "DK",
                        "DO",
                        "DZ",
                        "EC",
                        "EE",
                        "EG",
                        "ES",
                        "FI",
                        "FR",
                        "GB",
                        "GR",
                        "GT",
                        "HK",
                        "HN",
                        "HU",
                        "ID",
                        "IE",
                        "IL",
                        "IN",
                        "IS",
                        "IT",
                        "JO",
                        "JP",
                        "KW",
                        "LB",
                        "LI",
                        "LT",
                        "LU",
                        "LV",
                        "MA",
                        "MC",
                        "MT",
                        "MX",
                        "MY",
                        "NI",
                        "NL",
                        "NO",
                        "NZ",
                        "OM",
                        "PA",
                        "PE",
                        "PH",
                        "PL",
                        "PS",
                        "PT",
                        "PY",
                        "QA",
                        "RO",
                        "SA",
                        "SE",
                        "SG",
                        "SK",
                        "SV",
                        "TH",
                        "TN",
                        "TR",
                        "TW",
                        "US",
                        "UY",
                        "VN",
                        "ZA"
                    ],
                    "external_urls": {
                        "spotify": "https://open.spotify.com/album/6U8OJV1S81NyqpJQPAty5z"
                    },
                    "href": "https://api.spotify.com/v1/albums/6U8OJV1S81NyqpJQPAty5z",
                    "id": "6U8OJV1S81NyqpJQPAty5z",
                    "images": [
                        {
                            "height": 640,
                            "url": "https://i.scdn.co/image/ab67616d0000b273201bdd90b3dad5674f768367",
                            "width": 640
                        },
                        {
                            "height": 300,
                            "url": "https://i.scdn.co/image/ab67616d00001e02201bdd90b3dad5674f768367",
                            "width": 300
                        },
                        {
                            "height": 64,
                            "url": "https://i.scdn.co/image/ab67616d00004851201bdd90b3dad5674f768367",
                            "width": 64
                        }
                    ],
                    "name": "Live With The Plovdiv Psychotic Symphony",
                    "release_date": "2019-08-30",
                    "release_date_precision": "day",
                    "total_tracks": 24,
                    "type": "album",
                    "uri": "spotify:album:6U8OJV1S81NyqpJQPAty5z"
                },
                "artists": [
                    {
                        "external_urls": {
                            "spotify": "https://open.spotify.com/artist/3oBGGy1VOs6IHOa1ZdUx2f"
                        },
                        "href": "https://api.spotify.com/v1/artists/3oBGGy1VOs6IHOa1ZdUx2f",
                        "id": "3oBGGy1VOs6IHOa1ZdUx2f",
                        "name": "Sons Of Apollo",
                        "type": "artist",
                        "uri": "spotify:artist:3oBGGy1VOs6IHOa1ZdUx2f"
                    }
                ],
                "available_markets": [
                    "AD",
                    "AE",
                    "AR",
                    "AT",
                    "AU",
                    "BE",
                    "BG",
                    "BH",
                    "BO",
                    "BR",
                    "CA",
                    "CH",
                    "CL",
                    "CO",
                    "CR",
                    "CY",
                    "CZ",
                    "DE",
                    "DK",
                    "DO",
                    "DZ",
                    "EC",
                    "EE",
                    "EG",
                    "ES",
                    "FI",
                    "FR",
                    "GB",
                    "GR",
                    "GT",
                    "HK",
                    "HN",
                    "HU",
                    "ID",
                    "IE",
                    "IL",
                    "IN",
                    "IS",
                    "IT",
                    "JO",
                    "JP",
                    "KW",
                    "LB",
                    "LI",
                    "LT",
                    "LU",
                    "LV",
                    "MA",
                    "MC",
                    "MT",
                    "MX",
                    "MY",
                    "NI",
                    "NL",
                    "NO",
                    "NZ",
                    "OM",
                    "PA",
                    "PE",
                    "PH",
                    "PL",
                    "PS",
                    "PT",
                    "PY",
                    "QA",
                    "RO",
                    "SA",
                    "SE",
                    "SG",
                    "SK",
                    "SV",
                    "TH",
                    "TN",
                    "TR",
                    "TW",
                    "US",
                    "UY",
                    "VN",
                    "ZA"
                ],
                "disc_number": 1,
                "duration_ms": 271853,
                "explicit": False,
                "external_ids": {
                    "isrc": "GBDHC1909019"
                },
                "external_urls": {
                    "spotify": "https://open.spotify.com/track/5XzObBGRjIRkRzSmZZ6GjI"
                },
                "href": "https://api.spotify.com/v1/tracks/5XzObBGRjIRkRzSmZZ6GjI",
                "id": "5XzObBGRjIRkRzSmZZ6GjI",
                "is_local": False,
                "name": "Hell's Kitchen - Live at the Roman Amphitheatre in Plovdiv 2018",
                "popularity": 21,
                "preview_url": "https://p.scdn.co/mp3-preview/69363de04188574862ffe496106290227fd6073d?cid=774b29d4f13844c495f206cafdad9c86",
                "track_number": 19,
                "type": "track",
                "uri": "spotify:track:5XzObBGRjIRkRzSmZZ6GjI"
            },
            "currently_playing_type": "track",
            "actions": {
                "disallows": {
                    "resuming": True
                }
            },
            "is_playing": True
        }
        """
