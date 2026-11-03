"""Where the client keeps its settings and its cache. The server address lives only here, never in records."""
from __future__ import annotations

import json
import os
from pathlib import Path


def config_path() -> Path:
    if os.environ.get('FW_CONFIG'):
        return Path(os.environ['FW_CONFIG'])
    if os.name == 'nt':
        base = Path(os.environ.get('APPDATA') or Path.home() / 'AppData' / 'Roaming')
    else:
        base = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    return base / 'fw' / 'config.json'


def cache_dir() -> Path:
    if os.environ.get('FW_CACHE'):
        return Path(os.environ['FW_CACHE'])
    if os.name == 'nt':
        base = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local')
    else:
        base = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache')
    return base / 'fw'


def load() -> dict:
    """Saved settings, overridden by FW_SERVER and FW_TOKEN."""
    data = {}
    path = config_path()
    if path.is_file():
        data = json.loads(path.read_text(encoding='utf-8'))
    if os.environ.get('FW_SERVER'):
        data['server'] = os.environ['FW_SERVER']
    if os.environ.get('FW_TOKEN'):
        data['token'] = os.environ['FW_TOKEN']
    return data


def save(server: str, token: str) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump({'server': server, 'token': token}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path
