class UserLevelException(Exception):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

class NoActiveDevice(UserLevelException):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

class PremiumRequired(UserLevelException):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

class OtherError(UserLevelException):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

class NoContent(UserLevelException):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

class PlaybackPaused(UserLevelException):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
