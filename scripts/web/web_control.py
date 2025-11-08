#!/usr/bin/env python3
"""
USB Gadget Web Control Interface

A Flask web application for controlling USB gadget modes.
Organized using blueprints for better maintainability.
"""

from flask import Flask

# Import configuration
from config import SECRET_KEY, WEB_PORT, GADGET_DIR

# Flask app initialization
app = Flask(__name__)
app.secret_key = SECRET_KEY

# Register blueprints
from blueprints import mode_control_bp, videos_bp, lock_chimes_bp, light_shows_bp, analytics_bp

app.register_blueprint(mode_control_bp)
app.register_blueprint(videos_bp)
app.register_blueprint(lock_chimes_bp)
app.register_blueprint(light_shows_bp)
app.register_blueprint(analytics_bp)


if __name__ == "__main__":
    print(f"Starting Tesla USB Gadget Web Control")
    print(f"Gadget directory: {GADGET_DIR}")
    print(f"Access the interface at: http://0.0.0.0:{WEB_PORT}/")
    app.run(host="0.0.0.0", port=WEB_PORT, debug=False, threaded=True)