"""What every room has in common.

A room picks up job cards in one status (`takes`), works on one card at a
time, and moves it to a new status. Some rooms also go looking for new
material on a timer (`source`).
"""
import threading
import traceback

from ..llm import LLMError


class Wait(Exception):
    """The room can't work right now (budget, daily limit). The card goes back on the shelf."""


class Pause(Exception):
    """Something needs your attention (model down, settings missing). The card goes back
    on the shelf and the room pauses until you resume it."""


class Room:
    name = ""
    label = ""
    line = ""            # "shorts" or "clients"
    takes = None         # status of the cards this room works on
    source_every = 0     # seconds between source() runs; 0 = this room never sources

    def __init__(self, ctx):
        self.ctx = ctx
        self.cfg = ctx.cfg
        self.db = ctx.db
        self.llm = ctx.llm
        self.current = None      # id of the card in this room's hands
        self.waiting = None      # why the room is idle although cards are queued

    # --- helpers for subclasses ---
    def log(self, kind, message, job_id=None, cost=0.0, **data):
        self.db.add_event(self.name, job_id, kind, message, cost, data)

    def ask(self, task, system, prompt, context, job_id):
        provider, model = self.llm.route(task)
        budget = float(self.cfg.get("factory.daily_budget_usd", 0))
        if provider == "anthropic" and self.db.cost_today() >= budget:
            raise Wait(f"today's model budget of ${budget:.2f} is used up")
        try:
            ans = self.llm.ask_json(task, system, prompt, context)
        except LLMError as e:
            raise Pause(str(e)) from e
        self.log("model", f"{task}: answered by {ans.provider} {ans.model}", job_id, cost=ans.cost,
                 provider=ans.provider, model=ans.model)
        return ans.data

    # --- what subclasses override ---
    def blocked(self):
        """Return a reason string if the room should not pick up work now."""
        return None

    def source(self):
        pass

    def work(self, job):
        """Return (new_status, data_patch, message)."""
        raise NotImplementedError

    # --- the loop body ---
    def _set_waiting(self, reason):
        if reason != self.waiting:
            self.waiting = reason
            if reason:
                self.log("waiting", reason)

    def step(self, stop: threading.Event | None = None) -> bool:
        """Work one card. Returns True if a card was handled."""
        if self.takes is None or self.db.paused(self.name):
            return False
        reason = self.blocked()
        if reason:
            if self.db.claim_peek(self.line, self.takes):
                self._set_waiting(reason)
            return False
        job = self.db.claim(self.line, self.takes, self.name)
        if job is None:
            self._set_waiting(None)
            return False
        self.current = job["id"]
        self.log("picked", f"picked up “{job['title']}”", job["id"])
        pace = float(self.cfg.get("factory.pace_seconds", 0))
        if pace and stop is not None:
            stop.wait(pace)
        try:
            status, patch, message = self.work(job)
        except Wait as w:
            self.db.finish(job["id"], job["status"])
            self._set_waiting(str(w))
            return False
        except Pause as p:
            self.db.finish(job["id"], job["status"])
            self.db.set_paused(self.name, True)
            self.log("error", f"paused: {p}", job["id"])
            return False
        except Exception as e:  # a bug or bad data: the card fails, the room keeps going
            self.db.finish(job["id"], "failed", {"error": str(e)})
            self.log("error", f"failed: {e}", job["id"], trace=traceback.format_exc(limit=3))
            return True
        finally:
            self.current = None
        self.db.finish(job["id"], status, patch)
        self.log("moved", message, job["id"], to_status=status)
        self._set_waiting(None)
        return True
