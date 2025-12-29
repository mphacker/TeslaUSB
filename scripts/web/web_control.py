#!/usr/bin/env python3
"""
USB Gadget Web Control Interface

A Flask web application for controlling USB gadget modes.
Organized using blueprints for better maintainability.
"""

from flask import Flask
import os

# Import configuration
from config import SECRET_KEY, WEB_PORT, GADGET_DIR

# Flask app initialization
app = Flask(__name__)
app.secret_key = SECRET_KEY

# Production optimizations
app.config['USE_X_SENDFILE'] = False  # Disabled - requires nginx/apache
app.config['TEMPLATES_AUTO_RELOAD'] = False  # Disable template watching - saves memory

# Register blueprints
from blueprints import (
    mode_control_bp,
    videos_bp,
    lock_chimes_bp,
    light_shows_bp,
    wraps_bp,
    analytics_bp,
    cleanup_bp,
    api_bp,
    fsck_bp,
    captive_portal_bp,
    catch_all_redirect
)

app.register_blueprint(mode_control_bp)
app.register_blueprint(videos_bp)
app.register_blueprint(lock_chimes_bp)
app.register_blueprint(light_shows_bp)
app.register_blueprint(wraps_bp)
app.register_blueprint(analytics_bp)
app.register_blueprint(cleanup_bp)
app.register_blueprint(api_bp)
app.register_blueprint(fsck_bp)
# Register captive portal blueprint LAST to avoid conflicting with other routes
app.register_blueprint(captive_portal_bp)

# Add catch-all route for captive portal (must be last)
@app.route('/<path:path>')
def wildcard_redirect(path):
    result = catch_all_redirect(path)
    if result:
        return result
    # If catch_all_redirect returns None, let Flask handle it normally (404)
    from flask import abort
    abort(404)


if __name__ == "__main__":
    print(f"Starting Tesla USB Gadget Web Control")
    print(f"Gadget directory: {GADGET_DIR}")
    print(f"Access the interface at: http://0.0.0.0:{WEB_PORT}/")

    # Try to use Waitress if available, otherwise fall back to Flask dev server
    try:
        from waitress import serve
        print("Using Waitress production server")
        # 3 threads optimal for Pi Zero 2 W (4 cores, 512MB RAM) - saves memory vs 6 threads
        serve(app, host="0.0.0.0", port=WEB_PORT, threads=3, channel_timeout=300,
              send_bytes=4194304)  # 4MB send buffer for better video streaming
    except ImportError:
        print("Waitress not available, using Flask development server")
        print("WARNING: Flask dev server is slow for large files. Install waitress: pip3 install waitress")
        app.run(host="0.0.0.0", port=WEB_PORT, debug=False, threaded=True)
