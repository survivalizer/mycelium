"""What this build calls itself, and the release notes it ships with.

Separate from app.py so a route module can read them without importing the
Flask app: the admin Releases tab serves RELEASES and the SPA shell stamps
APP_VERSION into a meta tag. A release bumps APP_VERSION here."""
import json as _json
import os.path as _path

APP_VERSION = "1.1.0"

with open(_path.join(_path.dirname(__file__), "releases.json"), encoding="utf-8") as _f:
    RELEASES: list[dict] = _json.load(_f)
