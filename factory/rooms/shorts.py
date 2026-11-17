"""The video line: Trend Finder → Control Room → Video Maker → Review Room → (you) → Acc Manager."""
import json
import os
import shutil
import time
import urllib.parse
import urllib.request

from ..video import render_short
from .base import Pause, Room


def _clamp(n, lo, hi):
    return max(lo, min(hi, n))


class TrendFinder(Room):
    name, label, line, takes = "trend_finder", "Trend Finder", "shorts", "trend"
    source_every = 15

    def source(self):
        trends, origin = [], "sample list"
        key = os.environ.get("YOUTUBE_API_KEY")
        if key:
            trends, origin = self._youtube(key), "YouTube"
        else:
            trends = json.loads((self.cfg.root / "data/sample_trends.json").read_text())
        new = 0
        for t in trends:
            if self.db.create_job(self.line, "trend", t["title"], t, key=f"trend:{t['platform']}:{t['id']}"):
                new += 1
        if new:
            self.log("found", f"found {new} new trends on the {origin}")

    def _youtube(self, key):
        # Most popular videos in the region: costs 1 unit of the free 10,000 daily quota.
        q = urllib.parse.urlencode({"part": "snippet,statistics", "chart": "mostPopular", "maxResults": 25,
                                    "regionCode": self.cfg.get("channel.region", "US"), "key": key})
        try:
            with urllib.request.urlopen(f"https://www.googleapis.com/youtube/v3/videos?{q}", timeout=20) as r:
                items = json.loads(r.read()).get("items", [])
        except OSError as e:
            self.log("error", f"could not read YouTube trends: {e}")
            return []
        return [{"id": i["id"], "platform": "youtube", "title": i["snippet"]["title"],
                 "views": int(i.get("statistics", {}).get("viewCount", 0))} for i in items]

    def work(self, job):
        d = job["data"]
        niche = self.cfg.get("channel.niche", "")
        ctx = {"title": job["title"], "views": d.get("views", 0), "niche": niche}
        ans = self.ask(
            "score_trend",
            "You score social media trends for a short-video channel. Score how well a trend can be "
            "turned into a short video for this channel, from 0 (useless) to 100 (perfect fit). "
            'Return {"score": int, "angle": "one sentence: the video idea", "reason": "short reason"}.',
            f"Channel niche: {niche}\nTrend: {job['title']}\nPlatform: {d.get('platform')}\nViews: {d.get('views', 0)}",
            ctx, job["id"])
        score = _clamp(int(ans.get("score", 0)), 0, 100)
        reason = str(ans.get("reason", ""))
        return "idea", {"score": score, "angle": str(ans.get("angle", "")), "score_reason": reason}, \
            f"scored {score}: {reason}"


class ControlRoom(Room):
    name, label, line, takes = "control_room", "Control Room", "shorts", "idea"

    def blocked(self):
        cap = int(self.cfg.get("channel.max_videos_per_day", 4))
        if self.db.moves_today(self.name, "approved") >= cap:
            return f"daily limit of {cap} videos reached"
        return None

    def work(self, job):
        d = job["data"]
        minimum = int(self.cfg.get("channel.min_trend_score", 55))
        score = int(d.get("score", 0))
        # Rules first: weak ideas are turned down without paying for a model call.
        if score < minimum:
            return "rejected", {"decision": f"score {score} is below {minimum}"}, \
                f"turned down by rule: score {score} is below {minimum}"
        ans = self.ask(
            "decide",
            "You run a short-video channel's planning room. Decide whether this idea is worth making "
            "into a video today. Say no to ideas that are off-topic, risky, or copy someone else's content. "
            'Return {"go": bool, "reason": "short reason", "brief": {"angle": "the video idea", "length_seconds": int}}.',
            f"Channel niche: {self.cfg.get('channel.niche')}\nIdea: {job['title']}\n"
            f"Suggested angle: {d.get('angle')}\nTrend score: {score} ({d.get('score_reason')})",
            {"title": job["title"], "score": score, "min_score": minimum, "angle": d.get("angle")},
            job["id"])
        reason = str(ans.get("reason", ""))
        if ans.get("go"):
            return "approved", {"brief": ans.get("brief") or {}, "decision": reason}, f"approved: {reason}"
        return "rejected", {"decision": reason}, f"turned down: {reason}"


class VideoMaker(Room):
    name, label, line, takes = "video_maker", "Video Maker", "shorts", "approved"

    def work(self, job):
        d = job["data"]
        notes = d.get("review_notes")
        prompt = (f"Brief: {json.dumps(d.get('brief', {}))}\nTrend: {job['title']}\n"
                  f"Channel niche: {self.cfg.get('channel.niche')}")
        if notes:
            prompt += f"\nThe last draft failed review for these reasons, fix them: {notes}"
        script = self.ask(
            "write_script",
            "You write scripts for vertical short videos (YouTube Shorts and TikTok), 15 to 30 seconds. "
            "Each line appears on screen as large text, so keep each line under 90 characters. "
            'Return {"hook": "first line, under 50 characters", "lines": ["4 to 6 lines"], '
            '"caption": "post caption under 90 characters", "hashtags": ["#tag", ...]}.',
            prompt, {"title": job["title"]}, job["id"])
        lines = [str(x) for x in script.get("lines", []) if str(x).strip()]
        if not lines:
            raise ValueError("the script came back with no lines")
        script = {"hook": str(script.get("hook", "")), "lines": lines,
                  "caption": str(script.get("caption", "")),
                  "hashtags": [str(h) for h in script.get("hashtags", [])]}
        out = self.cfg.var / "videos" / f"job_{job['id']}.mp4"
        seconds = render_short(self.cfg, script, out)
        return "drafted", {"script": script, "video": out.name, "thumb": out.with_suffix(".jpg").name,
                           "duration": seconds}, f"rendered a {seconds:.0f}-second video"


class ReviewRoom(Room):
    name, label, line, takes = "review_room", "Review Room", "shorts", "drafted"

    def work(self, job):
        d = job["data"]
        s = d.get("script", {})
        problems = []
        video = self.cfg.var / "videos" / d.get("video", "")
        if not d.get("video") or not video.exists() or video.stat().st_size < 1000:
            problems.append("the video file is missing")
        if not 5 <= float(d.get("duration", 0)) <= 60:
            problems.append(f"length {d.get('duration', 0):.0f}s is outside 5 to 60 seconds")
        if not s.get("caption") or len(s.get("caption", "")) > 90:
            problems.append("the caption is empty or longer than 90 characters")
        if not s.get("hashtags"):
            problems.append("there are no hashtags")
        text = " ".join([s.get("hook", ""), s.get("caption", ""), *s.get("lines", [])]).lower()
        banned = [w for w in self.cfg.get("channel.banned_words", []) if w.lower() in text]
        if banned:
            problems.append("banned words: " + ", ".join(banned))
        if not problems:
            return "awaiting_you", {"review": "passed"}, "passed the checklist, waiting for your approval"
        tries = int(d.get("review_tries", 0)) + 1
        notes = "; ".join(problems)
        if tries >= 2:
            return "failed", {"review": notes, "review_tries": tries}, f"failed review twice: {notes}"
        return "approved", {"review_notes": notes, "review_tries": tries}, f"sent back to Video Maker: {notes}"


class AccManager(Room):
    name, label, line, takes = "acc_manager", "Acc Manager", "shorts", "ready"

    def work(self, job):
        if self.cfg.get("publishing.live"):
            raise Pause("live publishing is not connected yet. It needs the YouTube Data API (OAuth) and "
                        "TikTok Content Posting API (app approval). Set [publishing] live = false.")
        d = job["data"]
        s = d["script"]
        tags = " ".join(s["hashtags"])
        posts = {}
        for platform in self.cfg.get("channel.platforms", []):
            if platform == "youtube_shorts":
                post = {"title": s["caption"][:90] + " #Shorts", "description": f"{s['hook']}\n\n{tags}"}
            elif platform == "tiktok":
                post = {"caption": f"{s['caption']} {tags}"[:2200]}
            else:
                continue
            post.update(platform=platform, video=d["video"], prepared=time.strftime("%Y-%m-%d %H:%M"))
            path = self.cfg.var / "outbox/posts" / f"job_{job['id']}_{platform}.json"
            path.write_text(json.dumps(post, indent=2))
            posts[platform] = path.name
        shutil.copy(self.cfg.var / "videos" / d["video"], self.cfg.var / "outbox/posts" / d["video"])
        names = " and ".join(p.replace("_", " ").title() for p in posts)
        return "posted", {"posts": posts}, f"prepared posts for {names} in var/outbox/posts (test mode)"
