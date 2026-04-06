"""Flask blueprints for organizing routes."""

from .mode_control import mode_control_bp
from .videos import videos_bp
from .lock_chimes import lock_chimes_bp
from .light_shows import light_shows_bp
from .wraps import wraps_bp
from .analytics import analytics_bp
from .mapping import mapping_bp
from .cleanup import cleanup_bp
from .api import api_bp
from .fsck import fsck_bp
from .music import music_bp
from .media import media_bp
from .captive_portal import captive_portal_bp, catch_all_redirect
from .cloud_archive import cloud_archive_bp

__all__ = [
    'mode_control_bp',
    'videos_bp',
    'lock_chimes_bp',
    'light_shows_bp',
    'wraps_bp',
    'analytics_bp',
    'mapping_bp',
    'cleanup_bp',
    'api_bp',
    'fsck_bp',
    'music_bp',
    'media_bp',
    'captive_portal_bp',
    'catch_all_redirect',
    'cloud_archive_bp',
]
