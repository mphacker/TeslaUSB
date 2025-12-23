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

# Enable sendfile for efficient large file serving
app.config['USE_X_SENDFILE'] = False  # Disabled - requires nginx/apache

# Register blueprints
from blueprints import (
    mode_control_bp,
    videos_bp,
    lock_chimes_bp,
    light_shows_bp,
    analytics_bp,
    cleanup_bp,
    api_bp,
    fsck_bp
)

app.register_blueprint(mode_control_bp)
app.register_blueprint(videos_bp)
app.register_blueprint(lock_chimes_bp)
app.register_blueprint(light_shows_bp)
app.register_blueprint(analytics_bp)
app.register_blueprint(cleanup_bp)
app.register_blueprint(api_bp)
app.register_blueprint(fsck_bp)


if __name__ == "__main__":
    print(f"Starting Tesla USB Gadget Web Control")
    print(f"Gadget directory: {GADGET_DIR}")
    print(f"Access the interface at: http://0.0.0.0:{WEB_PORT}/")

    # Try to use Waitress if available, otherwise fall back to Flask dev server
    try:
        from waitress import serve
        print("Using Waitress production server")
        serve(app, host="0.0.0.0", port=WEB_PORT, threads=6, channel_timeout=300,
              send_bytes=1048576)  # 1MB send buffer
    except ImportError:
        print("Waitress not available, using Flask development server")
        print("WARNING: Flask dev server is slow for large files. Install waitress: pip3 install waitress")
        app.run(host="0.0.0.0", port=WEB_PORT, debug=False, threaded=True)
