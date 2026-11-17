"""Finding new leads with a web search (Brave Search API) and from your own list.

Search is optional and costs money per query. It stays off unless
[clients.search] enabled = true and BRAVE_API_KEY is in .env.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .instagram import handle_from

# Sites that are not a business you can pitch, or that you chose not to use.
SKIP_DOMAINS = ("amazon.", "ebay.", "etsy.com", "aliexpress.", "alibaba.", "walmart.", "wikipedia.org", "youtube.com",
                "pinterest.", "reddit.com", "facebook.com", "tiktok.com", "x.com", "twitter.com", "linkedin.com",
                "fiverr.com", "upwork.com", "medium.com", "quora.com", "google.", "apple.com", "shopify.com")


class SearchError(Exception):
    pass


def brave(query: str, count: int = 20) -> list[dict]:
    key = os.environ.get("BRAVE_API_KEY")
    if not key:
        raise SearchError("web search needs BRAVE_API_KEY in .env")
    q = urllib.parse.urlencode({"q": query, "count": count})
    req = urllib.request.Request(f"https://api.search.brave.com/res/v1/web/search?{q}",
                                 headers={"X-Subscription-Token": key, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise SearchError(f"Brave Search refused the query ({e.code}). Check the key and your plan.") from e
    except OSError as e:
        raise SearchError(f"could not reach Brave Search: {e}") from e
    return [{"url": x.get("url", ""), "title": x.get("title", "")} for x in data.get("web", {}).get("results", [])]


def lead_from(text: str, title: str = "") -> dict | None:
    """Turn a URL, Instagram link or @handle into a lead, or None if it is not one."""
    text = text.strip()
    if not text or text.startswith("#"):
        return None
    if text.startswith("@") or "instagram.com/" in text.lower():
        h = handle_from(text)
        return {"kind": "creator", "instagram": h, "name": title or f"@{h}", "url": f"https://instagram.com/{h}"} if h else None
    if text.startswith("sample:"):
        return {"kind": "brand", "url": text, "name": title or Path(text[7:]).stem}
    if not text.startswith(("http://", "https://")):
        text = "https://" + text
    host = urllib.parse.urlparse(text).netloc.lower()
    if not host or "." not in host or any(d in host for d in SKIP_DOMAINS):
        return None
    return {"kind": "brand", "url": text, "name": title or host.removeprefix("www.")}


def read_list(path: Path) -> list[dict]:
    """data/leads.txt: one website, Instagram link or @handle per line."""
    if not path.exists():
        return []
    leads = []
    for line in path.read_text().splitlines():
        lead = lead_from(line.split("  #")[0])
        if lead:
            lead["source"] = path.name
            leads.append(lead)
    return leads
