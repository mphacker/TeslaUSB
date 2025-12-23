"""
Captive Portal Blueprint

Handles captive portal detection and redirects for various operating systems.
When users connect to the AP, their devices automatically check for internet
connectivity by making HTTP requests to known endpoints. We intercept these
requests and show a branded splash screen.
"""

from flask import Blueprint, redirect, request, make_response, render_template
import logging
import os
import yaml

logger = logging.getLogger(__name__)

captive_portal_bp = Blueprint('captive_portal', __name__)

def get_ap_ssid():
    """Get the AP SSID from config.yaml"""
    try:
        # config.yaml is two levels up from this file (blueprints/ -> web/ -> root)
        config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'config.yaml')
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)
            return config.get('offline_ap', {}).get('ssid', 'TeslaUSB')
    except Exception:
        pass
    return 'TeslaUSB'

# List of common captive portal detection endpoints
# These are URLs that various operating systems check to detect captive portals
CAPTIVE_PORTAL_ENDPOINTS = [
    # Apple iOS/macOS
    '/hotspot-detect.html',
    '/library/test/success.html',

    # Android
    '/generate_204',
    '/gen_204',

    # Windows
    '/connecttest.txt',
    '/ncsi.txt',
    '/redirect',

    # Firefox
    '/success.txt',

    # Generic
    '/canonical.html',
]

@captive_portal_bp.route('/hotspot-detect.html')
@captive_portal_bp.route('/library/test/success.html')
def apple_captive_portal():
    """
    Apple devices check these URLs and expect a specific response.
    If they get anything other than "Success", they show the captive portal.
    Show a branded splash screen instead of auto-redirecting.
    """
    logger.info(f"Apple captive portal detection from {request.remote_addr}")
    return render_template('captive_portal.html', ssid=get_ap_ssid())

@captive_portal_bp.route('/generate_204')
@captive_portal_bp.route('/gen_204')
def android_captive_portal():
    """
    Android devices check this URL and expect a 204 No Content response.
    If they get anything else, they show the captive portal.
    Show the branded splash screen.
    """
    logger.info(f"Android captive portal detection from {request.remote_addr}")
    return render_template('captive_portal.html', ssid=get_ap_ssid())

@captive_portal_bp.route('/connecttest.txt')
@captive_portal_bp.route('/ncsi.txt')
@captive_portal_bp.route('/redirect')
def windows_captive_portal():
    """
    Windows checks these URLs for connectivity.
    Show the branded splash screen.
    """
    logger.info(f"Windows captive portal detection from {request.remote_addr}")
    return render_template('captive_portal.html', ssid=get_ap_ssid())

@captive_portal_bp.route('/success.txt')
@captive_portal_bp.route('/canonical.html')
def generic_captive_portal():
    """
    Firefox and other browsers check these endpoints.
    Show the branded splash screen.
    """
    logger.info(f"Generic captive portal detection from {request.remote_addr}")
    return render_template('captive_portal.html', ssid=get_ap_ssid())

@captive_portal_bp.route('/favicon.ico')
def favicon():
    """
    Return empty response for favicon to avoid 404s in logs
    """
    return '', 204

# Wildcard route to catch any other requests and redirect to main interface
# This must be registered with the app directly, not as a blueprint route
def catch_all_redirect(path):
    """
    Catch-all route for any URL not matching specific routes.
    This ensures any domain/path combination redirects to our interface.
    """
    # Skip if this is already the root or a static file
    if path == '' or path.startswith('static/'):
        return None

    # Check if this is a known API or page route
    known_prefixes = ['/videos', '/chimes', '/light_shows', '/analytics',
                      '/cleanup', '/api', '/fsck', '/mode', '/session']
    if any(path.startswith(prefix.lstrip('/')) for prefix in known_prefixes):
        return None

    logger.info(f"Captive portal catch-all redirect from {request.remote_addr}: /{path}")
    return redirect('/', code=302)
