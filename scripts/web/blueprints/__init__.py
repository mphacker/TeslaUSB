"""Flask blueprints for organizing routes."""

from .mode_control import mode_control_bp
from .videos import videos_bp
from .lock_chimes import lock_chimes_bp
from .light_shows import light_shows_bp
from .analytics import analytics_bp
from .cleanup import cleanup_bp
from .api import api_bp
from .fsck import fsck_bp
from .captive_portal import captive_portal_bp, catch_all_redirect

__all__ = [
    'mode_control_bp',
    'videos_bp',
    'lock_chimes_bp',
    'light_shows_bp',
    'analytics_bp',
    'cleanup_bp',
    'api_bp',
    'fsck_bp',
    'captive_portal_bp',
    'catch_all_redirect'
]
