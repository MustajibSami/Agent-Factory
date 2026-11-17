"""The 3D clients wing: Client Finder → Offer Writer → (you) → Outreach.

Finds brands (product pages with no 3D) and Instagram pages / creators,
finds the contact email each one published, drafts a short offer, and
sends it only after you approve it.
"""
import csv
import email
import email.policy
import os
import re
import smtplib
import time
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from pathlib import Path

from ..leads import instagram, search, web
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


def find_3d(html: str) -> list[str]:
    return [label for rx, label in SIGNALS if rx.search(html)]


class ClientFinder(Room):
    """Collects leads, finds the email each one published, and checks brands for 3D."""
    name, label, line, takes = "client_finder", "Client Finder", "clients", "prospect"
    source_every = 15

    def __init__(self, ctx):
        super().__init__(ctx)
        self.skipped = set()

    # --- finding new leads ---
    def source(self):
        c = self.cfg.get
        new = 0
        path = self.cfg.root / c("clients.prospects_file", "data/prospects.csv")
        if path.exists():
            with open(path, newline="") as f:
                new += sum(1 for row in csv.DictReader(f) if add_prospect(self, row))
        for lead in search.read_list(self.cfg.root / c("clients.leads_file", "data/leads.txt")):
            new += 1 if add_prospect(self, lead) else 0
        if new:
            self.log("found", f"found {new} new leads in your lists")
        if c("clients.search.enabled"):
            self.web_search()

    def web_search(self):
        c = self.cfg.get
        per_day = int(c("clients.search.queries_per_day", 5))
        cost = float(c("clients.search.cost_per_query", 0.005))
        done_today = {e["data"].get("query") for e in self.db.events_today(self.name, "searched")}
        for query in c("clients.search.queries", []):
            if query in done_today:
                continue
            if len(done_today) >= per_day:
                return
            if self.db.cost_today() + cost > float(c("factory.daily_budget_usd", 0)):
                self._set_waiting("today's budget is used up, no more web searches")
                return
            try:
                results = search.brave(query)
            except search.SearchError as e:
                self.log("error", str(e))
                return
            leads = [search.lead_from(r["url"], r["title"]) for r in results]
            added = sum(1 for lead in leads if lead and add_prospect(self, {**lead, "source": f"search: {query}"}))
            self.log("searched", f"searched “{query}”: {added} new leads", cost=cost, query=query)
            done_today.add(query)

    # --- researching one lead ---
    def work(self, job):
        d = dict(job["data"])
        if d.get("email") and self.db.is_suppressed(d["email"]):
            return "disqualified", {"reason": "on the do-not-contact list"}, "skipped: on the do-not-contact list"
        if d.get("kind") == "creator":
            verdict = self._creator(d)
        else:
            verdict = self._brand(d)
        if verdict:
            return "disqualified", {**d, "reason": verdict}, f"dropped: {verdict}"
        if self.db.is_suppressed(d["email"]):
            return "disqualified", {**d, "reason": "on the do-not-contact list"}, "skipped: on the do-not-contact list"
        if not self.db.claim_email(d["email"], job["id"]):
            return "disqualified", {**d, "reason": "this email is already on another card"}, \
                f"dropped: {d['email']} is already on another card"
        where = "your list" if d.get("found_on") == "given" else d.get("found_on")
        what = "creator" if d.get("kind") == "creator" else "no 3D on the page"
        return "qualified", d, f"good lead ({what}), email {d['email']} from {where}"

    def _creator(self, d):
        """Returns a reason to drop the lead, or None. Fills d in place."""
        try:
            profile = instagram.lookup(d["instagram"], self.cfg)
        except instagram.InstagramError as e:
            if not d.get("email"):
                return str(e)
            profile = {}
        d.update({"name": profile.get("name") or d.get("name"), "bio": profile.get("biography", ""),
                  "website": profile.get("website", ""), "followers": profile.get("followers_count")})
        minimum = int(self.cfg.get("clients.instagram.min_followers", 1000))
        if d["followers"] is not None and d["followers"] < minimum:
            return f"{d['followers']} followers, below your minimum of {minimum}"
        if d.get("email"):
            d.setdefault("found_on", "given")
            return None
        bio_emails = web.emails_in(d["bio"])
        if bio_emails:
            d["email"], d["found_on"] = bio_emails[0], f"Instagram bio of @{d['instagram']}"
            return None
        if d["website"]:
            try:
                d["email"], d["found_on"] = web.find_contact(d["website"], self.cfg.root)
            except (OSError, ValueError):
                pass
        return None if d.get("email") else "no public business email in the bio or on the website"

    def _brand(self, d):
        try:
            html = web.fetch(d["url"], self.cfg.root)
        except (OSError, ValueError) as e:
            return f"the page could not be read: {e}"
        found = find_3d(html)
        if found:
            d["signals"] = found
            return "already has 3D: " + ", ".join(found)
        if d.get("email"):
            d.setdefault("found_on", "given")
            return None
        try:
            d["email"], d["found_on"] = web.find_contact(d["url"], self.cfg.root, first_html=html)
        except (OSError, ValueError):
            pass
        return None if d.get("email") else "no published contact email on the site"


def add_prospect(room, row) -> int | None:
    """Create a lead card. Needs a website or Instagram handle; the email is optional
    (the Client Finder looks for a published one). Returns the job id, or None."""
    row = {k: (v or "").strip() if isinstance(v, str) or v is None else v for k, v in row.items() if k}
    addr = row.get("email", "").lower()
    lead = None
    if row.get("instagram"):
        lead = search.lead_from("@" + row["instagram"].lstrip("@"))
    elif row.get("url"):
        lead = search.lead_from(row["url"])
    if lead and row.get("kind") in ("brand", "creator") and lead["kind"] != row["kind"] and not lead.get("instagram"):
        lead["kind"] = row["kind"]
    if not lead or (addr and not EMAIL_RE.match(addr)):
        tag = (row.get("company") or row.get("name"), row.get("url"), addr)
        if tag not in room.skipped:
            room.skipped.add(tag)
            why = f"“{addr}” is not a valid email" if lead else "needs a website or Instagram handle"
            room.log("skipped", f"skipped {tag[0] or tag[1] or 'a row'}: {why}")
        return None
    name = row.get("company") or row.get("name") or lead["name"]
    data = {**lead, "name": name, "company": name, "product": row.get("product", ""),
            "email": addr, "source": row.get("source", "manual")}
    key = f"lead:{lead.get('instagram') or lead['url'].lower().rstrip('/')}"
    title = f"{name}: {data['product']}" if data["product"] else name
    return room.db.create_job("clients", "prospect", title, data, key=key)


class OfferWriter(Room):
    name, label, line, takes = "offer_writer", "Offer Writer", "clients", "qualified"

    BRAND = ("You write short, honest first-contact emails offering 3D product modelling to companies "
             "whose product pages show only photos. Mention their actual product, one concrete benefit "
             "(shoppers can turn the product around, view it in their room, fewer photo shoots), and offer "
             "a free sample model. Under 120 words. No hype, no false claims, no fake familiarity.")
    CREATOR = ("You write short, honest first-contact emails offering 3D visuals to Instagram pages and "
               "content creators: 3D product spins, animated 3D logos and intros, and 3D scenes for reels. "
               "Mention something specific from their profile, one concrete idea for their content, and offer "
               "a free short sample. Under 120 words. No hype, no flattery, no false claims.")

    def work(self, job):
        d = job["data"]
        creator = d.get("kind") == "creator"
        about = (f"Instagram page: @{d.get('instagram')} ({d.get('name')})\nBio: {d.get('bio', '')}\n"
                 f"Followers: {d.get('followers')}" if creator else
                 f"Company: {d.get('company')}\nProduct: {d.get('product') or 'unknown'}\nPage: {d.get('url')}")
        offer = self.ask(
            "write_offer", (self.CREATOR if creator else self.BRAND) +
            ' Return {"subject": "under 60 characters", "body": "email body without a signature"}.',
            about, {"kind": d.get("kind", "brand"), "company": d.get("name") or d.get("company"),
                    "product": d.get("product") or "products", "instagram": d.get("instagram")}, job["id"])
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

