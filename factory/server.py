"""The control panel's backend: a small web server over the job database.

The browser view only draws what this returns and sends your commands back.
"""
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .db import day_start
from .rooms.clients import add_prospect
from .runner import Factory

WEB = Path(__file__).resolve().parent.parent / "web"


def create_app(factory: Factory | None = None, run_workers: bool = True) -> FastAPI:
    fac = factory or Factory(config.load())

    @asynccontextmanager
    async def lifespan(app):
        if run_workers:
            fac.start()
        yield
        fac.shutdown()

    app = FastAPI(title="Agent Factory", lifespan=lifespan)
    app.state.factory = fac
    db, cfg = fac.db, fac.cfg

    def job_view(job, full=False):
        out = {"id": job["id"], "line": job["line"], "status": job["status"], "title": job["title"],
               "room": fac.room_for(job), "worker": job["worker"], "updated": job["updated"]}
        if full:
            out["data"] = job["data"]
        return out

    @app.get("/api/state")
    def state():
        rooms = []
        for r in fac.rooms.values():
            rooms.append({"name": r.name, "label": r.label, "line": r.line, "takes": r.takes,
                          "paused": db.paused(r.name), "current": r.current, "waiting": r.waiting,
                          "queue": db.queue_size(r.line, r.takes), "cost_today": round(db.cost_today(r.name), 4)})
        jobs = db.jobs(active_only=True)
        done_today = db._q("SELECT status, COUNT(*) n FROM jobs WHERE updated>=? AND status IN "
                           "('posted','sent','rejected','disqualified','failed') GROUP BY status",
                           (day_start(),)).fetchall()
        return {
            "mode": cfg.get("llm.mode", "mock"),
            "publishing_live": bool(cfg.get("publishing.live")),
            "outreach_live": bool(cfg.get("outreach.live")),
            "budget": float(cfg.get("factory.daily_budget_usd", 0)),
            "cost_today": round(db.cost_today(), 4),
            "rooms": rooms,
            "jobs": [job_view(j) for j in jobs],
            "awaiting": [job_view(j, full=True) for j in jobs if j["status"] == "awaiting_you"],
            "done_today": {row["status"]: row["n"] for row in done_today},
            "last_event_id": db.last_event_id(),
            "server_time": time.time(),
        }

    @app.get("/api/events")
    def events(after: int = 0, limit: int = 200):
        return db.events_after(after, min(limit, 500))

    @app.get("/api/jobs/{job_id}")
    def job(job_id: int):
        j = db.get_job(job_id)
        if not j:
            raise HTTPException(404, "no such job")
        return {**job_view(j, full=True), "events": db.events_after(job_id=job_id)}

    def _awaiting(job_id):
        j = db.get_job(job_id)
        if not j:
            raise HTTPException(404, "no such job")
        if j["status"] != "awaiting_you":
            raise HTTPException(409, f"this card is '{j['status']}', not waiting for you")
        return j

    @app.post("/api/jobs/{job_id}/approve")
    def approve(job_id: int):
        j = _awaiting(job_id)
        db.finish(job_id, "ready", {"approved_at": time.time()})
        db.add_event("you", job_id, "moved", f"you approved “{j['title']}”", data={"to_status": "ready"})
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/reject")
    def reject(job_id: int, note: str = Body("", embed=True)):
        j = _awaiting(job_id)
        db.finish(job_id, "rejected", {"rejected_note": note})
        db.add_event("you", job_id, "moved", f"you rejected “{j['title']}”" + (f": {note}" if note else ""),
                     data={"to_status": "rejected"})
        return {"ok": True}

    @app.post("/api/rooms/{name}/{action}")
    def room_action(name: str, action: str):
        if name not in fac.rooms or action not in ("pause", "resume"):
            raise HTTPException(404, "unknown room or action")
        db.set_paused(name, action == "pause")
        db.add_event("you", None, "control", f"you {'paused' if action == 'pause' else 'resumed'} "
                                               f"{fac.rooms[name].label}")
        return {"ok": True}

    @app.post("/api/ideas")
    def add_idea(title: str = Body(..., embed=True)):
        title = title.strip()
        if not 3 <= len(title) <= 200:
            raise HTTPException(422, "the idea needs 3 to 200 characters")
        jid = db.create_job("shorts", "trend", title, {"platform": "manual", "views": 0},
                            key=f"trend:manual:{title.lower()}")
        if not jid:
            raise HTTPException(409, "that idea is already in the factory")
        db.add_event("you", jid, "moved", f"you dropped in an idea: “{title}”", data={"to_status": "trend"})
        return {"id": jid}

    @app.post("/api/prospects")
    def add_prospect_route(row: dict = Body(...)):
        room = fac.rooms["client_finder"]
        jid = add_prospect(room, {**row, "source": "manual"})
        if not jid:
            raise HTTPException(422, "need a company, a product page address and a valid email "
                                     "(or this company is already in the factory)")
        db.add_event("you", jid, "moved", f"you added {row.get('company')}", data={"to_status": "prospect"})
        return {"id": jid}

    @app.get("/media/{name}")
    def media(name: str):
        path = cfg.var / "videos" / Path(name).name
        if not path.exists():
            raise HTTPException(404, "not found")
        return FileResponse(path)

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html")

    app.mount("/static", StaticFiles(directory=WEB), name="static")
    return app
