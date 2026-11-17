"""End-to-end tests in mock mode: no model calls, no network, no cost."""
import email
import email.policy

import pytest
from fastapi.testclient import TestClient

from factory import config
from factory.llm import LLMError, parse_json
from factory.rooms.clients import find_3d
from factory.runner import Factory
from factory.server import create_app


@pytest.fixture
def fac(tmp_path, monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    cfg = config.load(tmp_path)
    cfg.set("factory.pace_seconds", 0)
    cfg.set("llm.mode", "mock")
    cfg.set("publishing.live", False)
    cfg.set("outreach.live", False)
    cfg.set("video.width", 180)
    cfg.set("video.height", 320)
    cfg.set("video.fps", 8)
    cfg.set("video.seconds_per_line", 1.0)
    return Factory(cfg)


def drain(fac, *names):
    """Run the named rooms until none of them has work left."""
    while any(fac.rooms[n].step() for n in names):
        pass


def statuses(fac, line):
    rows = fac.db._q("SELECT title, status FROM jobs WHERE line=?", (line,)).fetchall()
    return {r["title"]: r["status"] for r in rows}


def test_shorts_line_end_to_end(fac):
    fac.cfg.set("channel.max_videos_per_day", 2)
    fac.rooms["trend_finder"].source()
    drain(fac, "trend_finder", "control_room", "video_maker", "review_room")
    waiting = [j for j in fac.db.jobs() if j["status"] == "awaiting_you"]
    assert len(waiting) == 2                                           # daily limit of 2 respected
    assert fac.rooms["control_room"].waiting == "daily limit of 2 videos reached"
    assert fac.db.count_status("idea") > 0                             # the rest wait for tomorrow

    fac.cfg.set("channel.max_videos_per_day", 99)                      # raise the limit: work resumes
    drain(fac, "control_room")
    s = statuses(fac, "shorts")
    assert s["I ate only gas station food for a week"] == "rejected"   # off-topic, turned down by rule
    assert fac.db.count_status("idea") == 0
    job = waiting[0]
    video = fac.cfg.var / "videos" / job["data"]["video"]
    assert video.stat().st_size > 1000 and video.with_suffix(".jpg").exists()

    client = TestClient(create_app(fac, run_workers=False))
    assert client.post(f"/api/jobs/{job['id']}/approve").status_code == 200
    assert client.post(f"/api/jobs/{job['id']}/approve").status_code == 409   # can't approve twice
    drain(fac, "acc_manager")
    assert fac.db.get_job(job["id"])["status"] == "posted"
    posts = sorted(p.name for p in (fac.cfg.var / "outbox/posts").glob(f"job_{job['id']}_*.json"))
    assert posts == [f"job_{job['id']}_tiktok.json", f"job_{job['id']}_youtube_shorts.json"]


def test_clients_wing_end_to_end(fac):
    fac.rooms["client_finder"].source()
    titles = statuses(fac, "clients")
    assert len(titles) == 4                                  # duplicate row and invalid email skipped
    drain(fac, "client_finder", "offer_writer")
    s = statuses(fac, "clients")
    assert s["Aerofit Shoes: Trail runner"] == "disqualified"      # has model-viewer
    assert s["Brightwell Watches: Field watch"] == "disqualified"  # has Sketchfab
    waiting = [j for j in fac.db.jobs() if j["status"] == "awaiting_you"]
    assert len(waiting) == 2
    assert 'reply "stop"' in waiting[0]["data"]["body"]

    client = TestClient(create_app(fac, run_workers=False))
    a, b = waiting
    client.post(f"/api/jobs/{a['id']}/approve")
    client.post(f"/api/jobs/{b['id']}/approve")
    fac.db.suppress(b["data"]["email"])                       # b replied stop before we sent
    drain(fac, "outreach")
    assert fac.db.get_job(a["id"])["status"] == "sent"
    assert fac.db.get_job(b["id"])["status"] == "disqualified"
    eml = fac.cfg.var / "outbox/emails" / f"job_{a['id']}.eml"
    msg = email.message_from_bytes(eml.read_bytes(), policy=email.policy.default)
    assert msg["To"] == a["data"]["email"] and msg["List-Unsubscribe"]


def test_reply_desk_sorts_replies(fac):
    inbox = fac.cfg.var / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "1.eml").write_text("From: a@shop.example\nSubject: Re: 3D\n\nNot interested, please stop.\n")
    (inbox / "2.eml").write_text("From: b@shop.example\nSubject: Re: 3D\n\nSounds good, how much?\n")
    fac.rooms["outreach"].source()
    assert fac.db.is_suppressed("a@shop.example")
    assert not fac.db.is_suppressed("b@shop.example")
    verdicts = [e["data"].get("verdict") for e in fac.db.events_after() if e["kind"] == "reply"]
    assert verdicts == ["stop", "interested"]
    assert not list(inbox.glob("*.eml"))


def test_outreach_daily_cap(fac):
    fac.cfg.set("outreach.daily_cap", 1)
    fac.rooms["client_finder"].source()
    drain(fac, "client_finder", "offer_writer")
    for j in fac.db.jobs():
        if j["status"] == "awaiting_you":
            fac.db.finish(j["id"], "ready")
    drain(fac, "outreach")
    assert fac.db.count_status("sent") == 1
    assert fac.db.count_status("ready") == 1
    assert fac.rooms["outreach"].waiting == "daily limit of 1 emails reached"


def test_budget_stops_paid_calls(fac):
    fac.cfg.set("llm.mode", "live")
    fac.cfg.set("factory.daily_budget_usd", 0.01)
    fac.db.add_event("trend_finder", None, "model", "earlier call", cost=0.02)
    fac.db.create_job("shorts", "trend", "A trend", {"platform": "manual", "views": 10})
    assert fac.rooms["trend_finder"].step() is False
    assert fac.db.count_status("trend") == 1                     # the card went back on the shelf
    assert "budget" in fac.rooms["trend_finder"].waiting


def test_model_error_pauses_room(fac):
    fac.cfg.set("llm.mode", "live")
    fac.cfg.set("llm.tasks.score_trend", {"provider": "ollama", "model": "x"})
    fac.cfg.set("llm.ollama_url", "http://127.0.0.1:9")          # nothing listens here
    fac.db.create_job("shorts", "trend", "A trend", {"platform": "manual", "views": 10})
    fac.rooms["trend_finder"].step()
    assert fac.db.paused("trend_finder")
    assert fac.db.count_status("trend") == 1


def test_manual_inputs_and_state(fac):
    client = TestClient(create_app(fac, run_workers=False))
    assert client.post("/api/ideas", json={"title": "Low-poly coffee cup turntable"}).status_code == 200
    assert client.post("/api/ideas", json={"title": "Low-poly coffee cup turntable"}).status_code == 409
    r = client.post("/api/prospects", json={"company": "Acme", "product": "Mug", "url": "https://acme.example/mug",
                                            "email": "bad"})
    assert r.status_code == 422
    client.post("/api/rooms/video_maker/pause")
    st = client.get("/api/state").json()
    rooms = {r["name"]: r for r in st["rooms"]}
    assert rooms["video_maker"]["paused"] and rooms["trend_finder"]["queue"] == 1
    assert st["jobs"][0]["room"] == "trend_finder"
    assert client.get("/").status_code == 200


def test_find_3d_and_parse_json():
    assert find_3d('<model-viewer src="a.glb">') == ["model-viewer 3D viewer", "3D model file (.glb/.gltf/.usdz)"]
    assert find_3d("<img src=a.jpg>") == []
    assert parse_json('Sure: {"a": 1} done') == {"a": 1}
    with pytest.raises(LLMError):
        parse_json("no json here")
