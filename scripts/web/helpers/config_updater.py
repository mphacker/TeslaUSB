"""
Shared config.yaml update utility.

Provides atomic writes to config.yaml from any blueprint or service.
Uses temp file + os.replace() for crash safety.
"""

import os
import yaml

from config import CONFIG_YAML


def update_config_yaml(updates: dict) -> None:
    """Atomically update config.yaml with new values.

    Args:
        updates: Dict of dotted-key paths to new values,
                 e.g. ``{'cloud_archive.max_upload_mbps': 10}``.
    """
    with open(CONFIG_YAML, 'r') as f:
        cfg = yaml.safe_load(f) or {}

    for key, value in updates.items():
        keys = key.split('.')
        d = cfg
        for k in keys[:-1]:
            d = d.setdefault(k, {})
        d[keys[-1]] = value

    tmp_path = CONFIG_YAML + '.tmp'
    with open(tmp_path, 'w') as f:
        yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, CONFIG_YAML)
