"""Hardware-bound encryption utilities for TeslaUSB.

Provides key derivation using the Pi's hardware identity (SoC serial +
machine-id) so encrypted credentials cannot be cloned to another device.
"""

import base64
import os

from config import GADGET_DIR

_KEY_CACHE = None


def _get_pi_serial() -> str:
    """Read Pi SoC serial number (hardware-bound, not on SD card)."""
    try:
        with open('/proc/cpuinfo', 'r') as f:
            for line in f:
                if line.startswith('Serial'):
                    return line.split(':')[1].strip()
    except Exception:
        pass
    return 'unknown-serial'


def _get_machine_id() -> str:
    """Read machine-id (unique per OS install)."""
    try:
        with open('/etc/machine-id', 'r') as f:
            return f.read().strip()
    except Exception:
        return 'unknown-machine'


def derive_encryption_key(pin: str = '') -> bytes:
    """Derive a Fernet encryption key from hardware identity + optional PIN.

    Key material combines: optional PIN, Pi serial number, and machine-id.
    Uses PBKDF2-HMAC-SHA256 with 600,000 iterations and a persistent
    random salt.

    Returns:
        bytes: URL-safe base64-encoded 32-byte key suitable for Fernet.
    """
    global _KEY_CACHE

    serial = _get_pi_serial()
    machine_id = _get_machine_id()
    key_material = f"{pin}:{serial}:{machine_id}".encode()

    salt_path = os.path.join(GADGET_DIR, 'tesla_salt.bin')
    if os.path.exists(salt_path):
        with open(salt_path, 'rb') as f:
            salt = f.read()
    else:
        salt = os.urandom(16)
        with open(salt_path, 'wb') as f:
            f.write(salt)
            f.flush()
            os.fsync(f.fileno())

    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=600_000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(key_material))
    _KEY_CACHE = key
    return key
