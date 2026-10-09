"""The network playback cache's view of where a song lives: the player's places.py (since 0.38.0 the
one classifier, so the player can ask the same question without the cache)."""

from ..places import *  # noqa: F401,F403
from ..places import (CLOUD_ATTRIBUTES, DRIVE_CACHE_S, DRIVE_REMOTE, Classifier, Kind, _connection,  # noqa: F401
                      _default, _dos_device, _drive_type, _letter, _logical_drives, _onedrive_roots, _stat,
                      _strip_long, _unc_of, classify, default, host_of, is_remote, network_target, source_key)
