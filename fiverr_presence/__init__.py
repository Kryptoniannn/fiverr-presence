"""fiverr_presence — check the publicly displayed online/offline status of Fiverr sellers."""

from .core import (
    CheckerConfig,
    FiverrPresenceChecker,
    PresenceResult,
    STATUS_OFFLINE,
    STATUS_ONLINE,
    STATUS_UNKNOWN,
    parse_target,
    profile_url,
)

__version__ = "1.0.0"
__all__ = [
    "CheckerConfig",
    "FiverrPresenceChecker",
    "PresenceResult",
    "STATUS_OFFLINE",
    "STATUS_ONLINE",
    "STATUS_UNKNOWN",
    "parse_target",
    "profile_url",
]
