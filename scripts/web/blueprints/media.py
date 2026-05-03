"""Blueprint for the unified media hub."""

import os

from flask import Blueprint, redirect, url_for

from config import IMG_LIGHTSHOW_PATH, IMG_MUSIC_PATH, MUSIC_ENABLED

media_bp = Blueprint('media', __name__, url_prefix='/media')


@media_bp.route("/")
def media_home():
    """Redirect to the first available media sub-page."""
    if os.path.isfile(IMG_LIGHTSHOW_PATH):
        return redirect(url_for('lock_chimes.lock_chimes'))
    if os.path.isfile(IMG_MUSIC_PATH) and MUSIC_ENABLED:
        return redirect(url_for('music.music_home'))
    # Fallback to lock_chimes even if not available (it will show the error message)
    return redirect(url_for('lock_chimes.lock_chimes'))

