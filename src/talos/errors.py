"""Safe diagnostics: messages never contain raw provider errors or bodies."""


class ConfigurationError(ValueError):
    """Invalid local configuration or command input."""


class AuthenticationError(RuntimeError):
    """Credentials are unavailable or rejected."""


class SynchronizationError(RuntimeError):
    """An API operation or source normalization could not be completed."""
