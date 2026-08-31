"""Define user-visible Spotify playback errors.

These exception types let the playback layer distinguish errors that can be
shown to listeners from unexpected implementation failures.
"""


class UserLevelException(Exception):
    """Base class for playback errors safe to show to a listener."""


class NoActiveDevice(UserLevelException):
    """Report that Spotify does not currently have an active device."""


class PremiumRequired(UserLevelException):
    """Report that the requested playback operation requires Spotify Premium."""


class OtherError(UserLevelException):
    """Report a Spotify playback error that has no more specific category."""


class NoContent(UserLevelException):
    """Report that Spotify returned no playable content for the request."""


class PlaybackPaused(UserLevelException):
    """Report that Spotify playback is currently paused."""
