"""Instagram profiles through Meta's official Business Discovery API.

It reads public business and creator profiles by username: name, bio,
website and follower count. It cannot search for accounts and does not
return an email, so the factory takes an email only from the bio or the
profile's website. No scraping and no direct messages.

Needs in .env: INSTAGRAM_USER_ID and INSTAGRAM_ACCESS_TOKEN (see README).
Handles starting with "sample_" read data/sample_pages instead (test mode).
"""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HANDLE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
NOT_PROFILES = {"p", "reel", "reels", "explore", "stories", "accounts", "tv", "about", "developer", "legal"}


class InstagramError(Exception):
    pass


def handle_from(text: str) -> str | None:
    """'@name', 'name' or an instagram.com/name link → 'name'."""
    text = text.strip()
    m = re.match(r"^(?:https?://)?(?:www\.)?instagram\.com/([^/?#]+)", text, re.I)
    h = m.group(1) if m else text.lstrip("@")
    h = h.strip().lower()
    return h if HANDLE.match(h) and h not in NOT_PROFILES else None


def configured() -> bool:
    return bool(os.environ.get("INSTAGRAM_USER_ID") and os.environ.get("INSTAGRAM_ACCESS_TOKEN"))


def lookup(handle: str, cfg) -> dict:
    if handle.startswith("sample_"):
        path = cfg.root / "data/sample_pages" / f"instagram_{handle}.json"
        if not path.exists():
            raise InstagramError(f"@{handle} not found")
        return json.loads(path.read_text())
    if not configured():
        raise InstagramError("Instagram lookups need INSTAGRAM_USER_ID and INSTAGRAM_ACCESS_TOKEN in .env")
    version = cfg.get("clients.instagram.graph_version", "v23.0")
    fields = f"business_discovery.username({handle}){{username,name,biography,website,followers_count}}"
    q = urllib.parse.urlencode({"fields": fields, "access_token": os.environ["INSTAGRAM_ACCESS_TOKEN"]})
    url = f"https://graph.facebook.com/{version}/{os.environ['INSTAGRAM_USER_ID']}?{q}"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", {}).get("message", "")
        except Exception:
            msg = ""
        raise InstagramError(f"Instagram said no for @{handle}: {msg or e.code}. "
                             "Only public business and creator accounts can be looked up.") from e
    except OSError as e:
        raise InstagramError(f"could not reach Instagram: {e}") from e
    return data.get("business_discovery", {})
