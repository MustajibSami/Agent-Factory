"""The 3D clients wing: Client Finder → Offer Writer → (you) → Outreach.

Finds companies that sell physical products, checks whether their product
page already has 3D, drafts a short offer for the ones that don't, and
sends it only after you approve it.
"""
import csv
import email
import email.policy
import os
import re
import smtplib
import time
import urllib.request
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from pathlib import Path

from .base import Pause, Room

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

# Signs that a product page already shows 3D.
SIGNALS = [
    (re.compile(r"<model-viewer", re.I), "model-viewer 3D viewer"),
    (re.compile(r"\.(glb|gltf|usdz)\b", re.I), "3D model file (.glb/.gltf/.usdz)"),
    (re.compile(r"sketchfab\.com", re.I), "Sketchfab embed"),
    (re.compile(r"three(\.module)?(\.min)?\.js|babylon(\.min)?\.js", re.I), "WebGL 3D library"),
    (re.compile(r'rel=["\']ar["\']|ar-modes|view in your (space|room)', re.I), "AR button"),
]

STOP_RE = re.compile(r"\b(stop|unsubscribe|remove me|not interested|do not (contact|email))\b", re.I)
YES_RE = re.compile(r"\b(interested|sounds good|let'?s talk|how much|price|quote|call|sample)\b", re.I)


def fetch_page(url: str, root: Path) -> str:
    if url.startswith("sample:"):
        return (root / "data/sample_pages" / Path(url[7:]).name).read_text()
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"not a web address: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; AgentFactory/0.1)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read(2_000_000).decode("utf-8", errors="replace")


def find_3d(html: str) -> list[str]:
    return [label for rx, label in SIGNALS if rx.search(html)]


class ClientFinder(Room):
    name, label, line, takes = "client_finder", "Client Finder", "clients", "prospect"
    source_every = 15

    def __init__(self, ctx):
        super().__init__(ctx)
        self.skipped = set()

    def source(self):
        path = self.cfg.root / self.cfg.get("clients.prospects_file", "data/prospects.csv")
        if not path.exists():
            return
        new = 0
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                if add_prospect(self, row):
                    new += 1
        if new:
            self.log("found", f"found {new} new companies in {path.name}")

    def work(self, job):
        d = job["data"]
        if self.db.is_suppressed(d["email"]):
            return "disqualified", {"reason": "on the do-not-contact list"}, "skipped: on the do-not-contact list"
        try:
            html = fetch_page(d["url"], self.cfg.root)
        except (OSError, ValueError) as e:
            return "disqualified", {"reason": f"page could not be read: {e}"}, f"could not read the page: {e}"
        found = find_3d(html)
        if found:
            return "disqualified", {"has_3d": True, "signals": found}, "already has 3D: " + ", ".join(found)
        return "qualified", {"has_3d": False}, "no 3D on the product page, a good lead"


def add_prospect(room, row) -> int | None:
    """Create a prospect card from a dict with company, product, url, email. Returns the job id."""
    row = {k: (v or "").strip() for k, v in row.items() if k}
    addr = row.get("email", "").lower()
    if not EMAIL_RE.match(addr) or not row.get("company") or not row.get("url"):
        tag = (row.get("company"), addr)
        if tag not in getattr(room, "skipped", set()):
            getattr(room, "skipped", set()).add(tag)
            room.log("skipped", f"skipped {row.get('company') or 'a row'}: missing company, page or a valid email")
        return None
    data = {"company": row["company"], "product": row.get("product") or "product", "url": row["url"],
            "email": addr, "source": row.get("source", "manual")}
    return room.db.create_job("clients", "prospect", f"{row['company']}: {data['product']}", data,
                              key=f"prospect:{addr}:{row['url'].lower()}")


class OfferWriter(Room):
    name, label, line, takes = "offer_writer", "Offer Writer", "clients", "qualified"

    def work(self, job):
        d = job["data"]
        offer = self.ask(
            "write_offer",
            "You write short, honest first-contact emails offering 3D product modelling to companies "
            "whose product pages show only photos. Mention their actual product, one concrete benefit, "
            "and offer a free sample model. Under 120 words. No hype, no false claims, no fake familiarity. "
            'Return {"subject": "under 60 characters", "body": "email body without a signature"}.',
            f"Company: {d['company']}\nProduct: {d['product']}\nProduct page: {d['url']}",
            {"company": d["company"], "product": d["product"]}, job["id"])
        c = self.cfg.get
        footer = (f"\n\n{c('outreach.sender_name')}\n{c('outreach.sender_business')}\n{c('outreach.sender_address')}\n\n"
                  "If you would rather not hear from me, reply \"stop\" and I won't email you again.")
        body = str(offer.get("body", "")).strip()
        if not body:
            raise ValueError("the offer came back empty")
        return "awaiting_you", {"subject": str(offer.get("subject", ""))[:80], "body": body + footer}, \
            "drafted an offer, waiting for your approval"


class Outreach(Room):
    name, label, line, takes = "outreach", "Outreach", "clients", "ready"
    source_every = 10   # the reply desk checks the inbox folder

    def blocked(self):
        cap = int(self.cfg.get("outreach.daily_cap", 10))
        if self.db.moves_today(self.name, "sent") >= cap:
            return f"daily limit of {cap} emails reached"
        return None

    def work(self, job):
        d = job["data"]
        if self.db.is_suppressed(d["email"]):
            return "disqualified", {"reason": "asked not to be contacted"}, "not sent: on the do-not-contact list"
        c = self.cfg.get
        msg = EmailMessage()
        msg["From"] = formataddr((c("outreach.sender_name"), c("outreach.reply_to")))
        msg["To"] = d["email"]
        msg["Reply-To"] = c("outreach.reply_to")
        msg["Subject"] = d["subject"]
        msg["List-Unsubscribe"] = f"<mailto:{c('outreach.reply_to')}?subject=stop>"
        msg.set_content(d["body"])
        if not c("outreach.live"):
            path = self.cfg.var / "outbox/emails" / f"job_{job['id']}.eml"
            path.write_bytes(bytes(msg))
            return "sent", {"sent_at": time.time(), "eml": path.name}, \
                f"wrote the email to var/outbox/emails/{path.name} (test mode, not sent)"
        env = os.environ
        if not env.get("SMTP_HOST") or not env.get("SMTP_USER"):
            raise Pause("live sending needs SMTP_HOST, SMTP_USER and SMTP_PASSWORD in .env")
        try:
            with smtplib.SMTP(env["SMTP_HOST"], int(env.get("SMTP_PORT", 587)), timeout=30) as s:
                s.starttls()
                s.login(env["SMTP_USER"], env.get("SMTP_PASSWORD", ""))
                s.send_message(msg)
        except (OSError, smtplib.SMTPException) as e:
            raise Pause(f"the mail server refused: {e}") from e
        return "sent", {"sent_at": time.time()}, f"sent to {d['email']}"

    # --- reply desk ---
    def source(self):
        """Reads replies saved as .eml or .txt files in var/inbox and sorts them."""
        inbox = self.cfg.var / "inbox"
        done = inbox / "done"
        done.mkdir(parents=True, exist_ok=True)
        for path in sorted(inbox.glob("*")):
            if path.suffix.lower() not in (".eml", ".txt"):
                continue
            msg = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)
            sender = parseaddr(msg.get("From", ""))[1].lower()
            part = msg.get_body(preferencelist=("plain",)) if msg.is_multipart() else msg
            text = (msg.get("Subject", "") + "\n" + (part.get_content() if part else "")).strip()
            if not sender:
                self.log("reply", f"{path.name}: no sender address found, left for you to read")
            elif STOP_RE.search(text):
                self.db.suppress(sender)
                self.log("reply", f"{sender} asked not to be contacted, added to the do-not-contact list",
                         verdict="stop", sender=sender)
            elif YES_RE.search(text):
                self.log("reply", f"{sender} sounds interested, follow up yourself", verdict="interested",
                         sender=sender, excerpt=text[:300])
            else:
                self.log("reply", f"{sender} replied, read it yourself", verdict="other", sender=sender,
                         excerpt=text[:300])
            path.rename(done / path.name)

