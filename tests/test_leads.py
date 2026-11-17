"""Finding clients: lead lists, published emails, Instagram pages, web search. All offline."""
import pytest

from factory import config
from factory.leads import search, web
from factory.leads.instagram import handle_from
from factory.runner import Factory


@pytest.fixture
def fac(tmp_path, monkeypatch):
    for k in ("YOUTUBE_API_KEY", "BRAVE_API_KEY", "INSTAGRAM_USER_ID", "INSTAGRAM_ACCESS_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    cfg = config.load(tmp_path)
    cfg.set("factory.pace_seconds", 0)
    cfg.set("llm.mode", "mock")
    cfg.set("clients.prospects_file", "data/no_such_file.csv")     # only data/leads.txt
    return Factory(cfg)


def research(fac):
    fac.rooms["client_finder"].source()
    while fac.rooms["client_finder"].step():
        pass
    rows = fac.db._q("SELECT * FROM jobs WHERE line='clients'").fetchall()
    jobs = [fac.db.get_job(r["id"]) for r in rows]
    return {j["data"].get("instagram") or j["data"]["url"]: j for j in jobs}


def test_lead_list_end_to_end(fac):
    cards = research(fac)
    brand = cards["sample:pinewood_home.html"]
    assert brand["status"] == "qualified"
    assert brand["data"]["email"] == "hello@pinewood.example"            # mailto on the contact page
    assert brand["data"]["found_on"] == "sample:pinewood_contact.html"

    clay = cards["sample_clayforms"]
    assert clay["status"] == "qualified" and clay["data"]["kind"] == "creator"
    assert clay["data"]["email"] == "studio@clayforms.example"           # "[at]"/"[dot]" on its website
    assert clay["data"]["found_on"] == "sample:clayforms_contact.html"

    render = cards["sample_studiorender"]
    assert render["data"]["email"] == "bookings@studiorender.example"   # from the Instagram bio
    assert render["data"]["found_on"] == "Instagram bio of @sample_studiorender"

    assert cards["sample_tinycandles"]["status"] == "disqualified"            # 312 followers < 1000
    assert "below your minimum" in cards["sample_tinycandles"]["data"]["reason"]
    assert cards["sample_noemail"]["status"] == "disqualified"         # never guesses an address
    assert cards["sample_noemail"]["data"].get("email") in ("", None)


def test_offers_differ_for_brands_and_creators(fac):
    research(fac)
    while fac.rooms["offer_writer"].step():
        pass
    drafts = {j["data"]["kind"]: j["data"] for j in fac.db.jobs() if j["status"] == "awaiting_you"}
    assert "3D visuals" in drafts["creator"]["subject"]
    assert "3D model" in drafts["brand"]["subject"]
    assert all('reply "stop"' in d["body"] for d in drafts.values())


def test_same_email_only_once(fac):
    fac.rooms["client_finder"].source()
    room = fac.rooms["client_finder"]
    from factory.rooms.clients import add_prospect
    add_prospect(room, {"url": "sample:pinewood_contact.html", "name": "Pinewood again"})
    research(fac)
    owners = [j for j in fac.db._q("SELECT * FROM jobs WHERE status='qualified'").fetchall()]
    emails = [fac.db.get_job(j["id"])["data"]["email"] for j in owners]
    assert emails.count("hello@pinewood.example") == 1
    again = [fac.db.get_job(r["id"]) for r in fac.db._q("SELECT id FROM jobs WHERE title='Pinewood again'")][0]
    assert again["status"] == "disqualified" and "already on another card" in again["data"]["reason"]


def test_do_not_contact_list_blocks_found_email(fac):
    fac.db.suppress("hello@pinewood.example")
    cards = research(fac)
    brand = cards["sample:pinewood_home.html"]
    assert brand["status"] == "disqualified"


def test_emails_in_filters_junk_and_reads_obfuscated():
    html = ('<a href="mailto:Hello@Shop.example?subject=hi">mail</a> logo@2x.png noreply@shop.example '
            'sales [at] shop [dot] example  user@example.com')
    assert web.emails_in(html) == ["hello@shop.example", "sales@shop.example"]
    assert web.best_email(["info@other.example", "orders@shop.example", "hello@shop.example"],
                          "https://www.shop.example/p/1") == "hello@shop.example"


def test_lead_parsing():
    assert handle_from("https://www.instagram.com/Some.Brand/?hl=en") == "some.brand"
    assert handle_from("https://instagram.com/p/Cx12/") is None              # a post, not a page
    assert search.lead_from("@studio")["kind"] == "creator"
    assert search.lead_from("shop.example/products/chair")["url"] == "https://shop.example/products/chair"
    for skipped in ("https://www.amazon.com/dp/1", "https://www.upwork.com/jobs/x", "https://fiverr.com/u", "# note"):
        assert search.lead_from(skipped) is None


def test_web_search_respects_daily_limit_and_budget(fac, monkeypatch):
    fac.cfg.set("clients.leads_file", "data/no_such_file.txt")
    fac.cfg.set("clients.search.enabled", True)
    fac.cfg.set("clients.search.queries", ["a", "b", "c"])
    fac.cfg.set("clients.search.queries_per_day", 2)
    calls = []

    def fake_brave(q, count=20):
        calls.append(q)
        return [{"url": f"https://{q}-shop.example/", "title": f"{q} shop"},
                {"url": "https://www.instagram.com/" + q + "_studio/", "title": "IG"},
                {"url": "https://www.etsy.com/shop/x", "title": "etsy"}]
    monkeypatch.setattr(search, "brave", fake_brave)
    room = fac.rooms["client_finder"]
    room.source()
    room.source()                                      # second run the same day: no new searches
    assert calls == ["a", "b"]
    assert fac.db.count_status("prospect") == 4        # 2 shops + 2 Instagram pages, Etsy skipped
    assert round(fac.db.cost_today(), 3) == 0.01       # searches count toward the spend meter

    fac.cfg.set("clients.search.queries_per_day", 5)
    fac.cfg.set("factory.daily_budget_usd", 0.012)     # only room for 0.002 more: "c" is not searched
    room.source()
    assert calls == ["a", "b"]
    assert "budget" in room.waiting


def test_add_form_accepts_instagram_and_website(fac):
    from fastapi.testclient import TestClient
    from factory.server import create_app
    client = TestClient(create_app(fac, run_workers=False))
    assert client.post("/api/prospects", json={"instagram": "@sample_clayforms"}).status_code == 200
    assert client.post("/api/prospects", json={"url": "shop.example", "email": "bad"}).status_code == 422
    assert client.post("/api/prospects", json={"url": "https://shop.example"}).status_code == 200
    assert client.post("/api/prospects", json={"url": "https://shop.example/"}).status_code == 422   # duplicate
