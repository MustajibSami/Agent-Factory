"""Reading web pages and finding the contact email a business published.

Rule: only take an email the business or creator put in public for contact
(a mailto link, the contact or about page, an Instagram bio). Never guess
addresses like info@domain, and always record where the email was found.
"""
import re
import urllib.parse
import urllib.request
from pathlib import Path

UA = "Mozilla/5.0 (compatible; AgentFactory/0.1)"
EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}")
MAILTO = re.compile(r'href=["\']mailto:([^"\'?]+)', re.I)
HREF = re.compile(r'href=["\']([^"\'#]+)["\']', re.I)
CONTACT_WORDS = re.compile(r"contact|about|impressum|press|wholesale|partner|collab|business|work-with", re.I)
JUNK = re.compile(r"\.(png|jpe?g|gif|webp|svg|avif)$|@2x|noreply|no-reply|donotreply|sentry|wixpress|"
                  r"@example\.(com|org)$|@domain\.com$|@email\.com$|yourname@|user@", re.I)
ROLE_ORDER = ["hello", "contact", "business", "partnerships", "collab", "studio", "info", "sales", "press", "team"]


def fetch(url: str, root: Path) -> str:
    """Download a page. `sample:name.html` reads data/sample_pages (test mode, no network)."""
    if url.startswith("sample:"):
        return (root / "data/sample_pages" / Path(url[7:]).name).read_text()
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"not a web address: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read(2_000_000).decode("utf-8", errors="replace")


def resolve(base: str, href: str) -> str | None:
    if base.startswith("sample:"):
        return None if ":" in href else "sample:" + Path(href).name
    url = urllib.parse.urljoin(base, href)
    if urllib.parse.urlparse(url).netloc != urllib.parse.urlparse(base).netloc:
        return None   # stay on the same website
    return url


def emails_in(html: str) -> list[str]:
    text = re.sub(r"\s*[\[(]\s*at\s*[\])]\s*", "@", html, flags=re.I)
    text = re.sub(r"\s*[\[(]\s*dot\s*[\])]\s*", ".", text, flags=re.I)
    found = [m.strip() for m in MAILTO.findall(html)] + EMAIL_IN_TEXT.findall(text)
    out = []
    for e in found:
        e = urllib.parse.unquote(e).strip().strip(".").lower()
        if EMAIL_IN_TEXT.fullmatch(e) and not JUNK.search(e) and e not in out:
            out.append(e)
    return out


def best_email(emails: list[str], site: str = "") -> str | None:
    """Prefer an address on the site's own domain, then common contact names."""
    host = urllib.parse.urlparse(site).netloc.lower().removeprefix("www.")

    def rank(e):
        local, domain = e.split("@", 1)
        same = 0 if host and (domain == host or domain.endswith("." + host)) else 1
        role = ROLE_ORDER.index(local) if local in ROLE_ORDER else len(ROLE_ORDER)
        return (same, role)
    return min(emails, key=rank) if emails else None


def find_contact(url: str, root: Path, first_html: str | None = None, max_pages: int = 3):
    """Look for a published email on a page and up to `max_pages` contact/about pages
    of the same site. Returns (email, page_it_was_found_on) or (None, None)."""
    html = first_html if first_html is not None else fetch(url, root)
    found = emails_in(html)
    if found:
        return best_email(found, url), url
    seen = {url}
    for href in HREF.findall(html):
        if not CONTACT_WORDS.search(href):
            continue
        page = resolve(url, href)
        if not page or page in seen:
            continue
        seen.add(page)
        if len(seen) > max_pages + 1:
            break
        try:
            found = emails_in(fetch(page, root))
        except (OSError, ValueError):
            continue
        if found:
            return best_email(found, url), page
    return None, None
