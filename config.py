"""
config.py - settings, secret storage and usage counters.

SECURITY MODEL
  * Everything private lives in a per-user folder OUTSIDE the project:
        Windows: %APPDATA%\\LocalVideoStudio      Linux/Mac: ~/.local/share/LocalVideoStudio
    (settings.json, secrets.json, token.json, client_secrets.json, usage.json,
     uploads.json). Zipping or sharing the project folder therefore never
    includes API keys or YouTube tokens.
  * API keys are encrypted with Windows DPAPI (tied to your Windows login) via
    ctypes - no extra package. On other systems they are stored with 0600
    permissions (not encrypted) unless the `keyring` package is installed.
  * Environment variables override stored keys (GEMINI_API_KEY, LVS_<NAME>_KEY).
"""
import base64
import copy
import datetime
import json
import os
import shutil
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))


def _data_dir():
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    path = os.path.join(base or os.path.expanduser("~"), "LocalVideoStudio")
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


DATA_DIR = _data_dir()


def data_path(name):
    return os.path.join(DATA_DIR, name)


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, obj, private=False):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)
    if private:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


# --------------------------------------------------------------------------
# One-time move of old files out of the project folder
# --------------------------------------------------------------------------
def migrate_legacy():
    """Move settings.json / token.json / client_secrets.json out of the project folder."""
    moved = []
    for name in ("settings.json", "token.json", "client_secrets.json"):
        src = os.path.join(APP_DIR, name)
        if os.path.exists(src) and not os.path.exists(data_path(name)):
            shutil.move(src, data_path(name))
            moved.append(name)
    return moved


# --------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------
def _dpapi(data, protect):
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    inb = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(inb), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def secret_backend():
    """'dpapi', 'keyring' or 'plain' - shown in Settings so you know how keys are kept."""
    if sys.platform == "win32":
        return "dpapi"
    try:
        import keyring  # noqa: F401
        return "keyring"
    except ImportError:
        return "plain"


def set_secret(name, value):
    store = _read_json(data_path("secrets.json"), {})
    value = (value or "").strip()
    if not value:
        store.pop(name, None)
    elif sys.platform == "win32":
        store[name] = "dpapi:" + base64.b64encode(_dpapi(value.encode("utf-8"), True)).decode()
    elif secret_backend() == "keyring":
        import keyring
        keyring.set_password("LocalVideoStudio", name, value)
        store[name] = "keyring:"
    else:
        store[name] = "plain:" + base64.b64encode(value.encode("utf-8")).decode()
    _write_json(data_path("secrets.json"), store, private=True)


def get_secret(name):
    env = os.environ.get(f"LVS_{name.upper()}_KEY") or (os.environ.get("GEMINI_API_KEY") if name == "gemini" else None)
    if env:
        return env
    raw = _read_json(data_path("secrets.json"), {}).get(name, "")
    try:
        if raw.startswith("dpapi:"):
            return _dpapi(base64.b64decode(raw[6:]), False).decode("utf-8")
        if raw.startswith("keyring:"):
            import keyring
            return keyring.get_password("LocalVideoStudio", name) or ""
        if raw.startswith("plain:"):
            return base64.b64decode(raw[6:]).decode("utf-8")
    except Exception:
        return ""
    return ""


def has_secret(name):
    return bool(get_secret(name))


# --------------------------------------------------------------------------
# Settings (non-secret)
# --------------------------------------------------------------------------
DEFAULTS = {
    "provider_order": ["gemini", "openai_compat", "ollama"],
    "providers": {
        "gemini": {"enabled": True, "model": "gemini-2.5-flash", "daily_limit": 200,
                   "base_url": "https://generativelanguage.googleapis.com/v1beta"},
        "openai_compat": {"enabled": False, "model": "llama-3.3-70b-versatile", "daily_limit": 200,
                          "base_url": "https://api.groq.com/openai/v1"},
        "ollama": {"enabled": False, "model": "llama3.2:3b", "daily_limit": 0,
                   "base_url": "http://localhost:11434"},
    },
    "whisper_model": "Off",       # Off / tiny / base / small (local transcription for files without captions)
    "language": "en",
    "region": "US",               # for trend research
}


def _merge(base, extra):
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load_settings():
    raw = _read_json(data_path("settings.json"), {})
    legacy_key = raw.pop("gemini_key", None)          # old versions stored the key in plain text
    legacy_model = raw.pop("gemini_model", None)
    if legacy_key is not None or legacy_model is not None:
        if legacy_key:
            set_secret("gemini", legacy_key)
        if legacy_model:
            raw.setdefault("providers", {}).setdefault("gemini", {})["model"] = legacy_model
        _write_json(data_path("settings.json"), raw, private=True)
    return _merge(copy.deepcopy(DEFAULTS), raw)


def save_settings(settings):
    _write_json(data_path("settings.json"), settings, private=True)


# --------------------------------------------------------------------------
# Daily usage counters (for provider limits)
# --------------------------------------------------------------------------
def _today():
    return datetime.date.today().isoformat()


def usage_count(provider):
    return _read_json(data_path("usage.json"), {}).get(_today(), {}).get(provider, 0)


def usage_add(provider):
    data = _read_json(data_path("usage.json"), {})
    day = data.get(_today(), {})
    day[provider] = day.get(provider, 0) + 1
    _write_json(data_path("usage.json"), {_today(): day})   # old days are dropped


# --------------------------------------------------------------------------
# Generic JSON helpers for other modules (uploads log etc.)
# --------------------------------------------------------------------------
def read_data_json(name, default):
    return _read_json(data_path(name), default)


def write_data_json(name, obj):
    _write_json(data_path(name), obj, private=True)
