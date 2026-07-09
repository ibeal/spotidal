class AuthenticationError(Exception):
    """Raised when authentication with a music provider fails."""


class SyncAbortError(Exception):
    """Raised when sync cannot continue due to unrecoverable API errors."""


class RateLimitAbortError(SyncAbortError):
    """Raised when an API retry delay exceeds the configured maximum wait."""
