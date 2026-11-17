"""Builds the factory and runs one worker thread per room."""
import threading
import time
from dataclasses import dataclass

from .config import Config
from .db import DB
from .llm import LLM
from .rooms.clients import ClientFinder, OfferWriter, Outreach
from .rooms.shorts import AccManager, ControlRoom, ReviewRoom, TrendFinder, VideoMaker

ROOM_CLASSES = [TrendFinder, ControlRoom, VideoMaker, ReviewRoom, AccManager, ClientFinder, OfferWriter, Outreach]


@dataclass
class Context:
    cfg: Config
    db: DB
    llm: LLM


class Factory:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = DB(cfg.db_path)
        self.ctx = Context(cfg, self.db, LLM(cfg))
        self.rooms = {cls.name: cls(self.ctx) for cls in ROOM_CLASSES}
        self.stop = threading.Event()
        self.threads = []

    def room_for(self, job):
        """Which room a card is in right now, for drawing."""
        if job["worker"]:
            return job["worker"]
        if job["status"] == "awaiting_you":
            return "you"
        for r in self.rooms.values():
            if r.line == job["line"] and r.takes == job["status"]:
                return r.name
        return None

    def _loop(self, room):
        next_source = 0.0
        every = room.source_every or float(self.cfg.get("factory.source_every_seconds", 15))
        while not self.stop.is_set():
            if room.source_every and time.time() >= next_source and not self.db.paused(room.name):
                try:
                    room.source()
                except Exception as e:
                    room.log("error", f"looking for new material failed: {e}")
                next_source = time.time() + every
            try:
                handled = room.step(self.stop)
            except Exception as e:  # never let one room's thread die
                room.log("error", f"room crashed and restarted: {e}")
                handled = False
            if not handled:
                self.stop.wait(1.0)

    def start(self):
        for room in self.rooms.values():
            t = threading.Thread(target=self._loop, args=(room,), name=room.name, daemon=True)
            t.start()
            self.threads.append(t)

    def shutdown(self):
        self.stop.set()
        for t in self.threads:
            t.join(timeout=5)
