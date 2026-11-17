"""Canned answers so the whole factory runs with no model and no cost.

These are simple rules, not intelligence. They exist so you can watch the
pipeline move and test the plumbing before paying for real models.
"""
import math
import re


def _words(text):
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def answer(task: str, c: dict) -> dict:
    if task == "score_trend":
        overlap = _words(c["title"]) & (_words(c["niche"]) | {"3d", "render", "renders", "model", "ar", "blender", "product", "animation"})
        reach = min(30, int(math.log10(max(c.get("views", 1), 10)) * 4))
        score = min(100, 25 + reach + 18 * len(overlap))
        fit = "fits the channel" if overlap else "off-topic for the channel"
        return {"score": score, "angle": f"Show how this looks done in 3D: {c['title']}", "reason": f"{fit}; {c.get('views', 0):,} views"}

    if task == "decide":
        go = c["score"] >= c["min_score"]
        return {"go": go, "reason": f"score {c['score']} vs minimum {c['min_score']}",
                "brief": {"angle": c.get("angle", c["title"]), "length_seconds": 20}}

    if task == "write_script":
        title = c["title"]
        return {
            "hook": "Stop scrolling. Watch this become 3D.",
            "lines": [
                f"Everyone is talking about: {title}",
                "Here is the same idea as a real 3D model.",
                "One model gives you every angle, every colour, no photo shoot.",
                "Shoppers can spin it, zoom it, and place it in their room.",
                "Follow for more 3D product breakdowns.",
            ],
            "caption": f"{title[:70]} | made in 3D",
            "hashtags": ["#3d", "#productdesign", "#blender", "#shorts"],
        }

    if task == "write_offer" and c.get("kind") == "creator":
        return {
            "subject": "3D visuals for your reels",
            "body": (
                f"Hi {c['company']},\n\n"
                "I make 3D visuals for creators: product spins, animated logos and short 3D scenes "
                "that stand out in a feed. One idea for your page: a 5-second 3D intro built around "
                "what you already post.\n\n"
                "I would be glad to make a free short sample so you can see it before deciding anything. "
                "Would that be useful?"
            ),
        }

    if task == "write_offer":
        return {
            "subject": f"A 3D model of your {c['product']}",
            "body": (
                f"Hi {c['company']} team,\n\n"
                f"I was looking at your {c['product']} and noticed the page shows photos only. "
                "A 3D model lets shoppers turn the product around and view it in their own space, "
                "and the same model can replace future photo shoots.\n\n"
                f"I would be glad to build a sample model of the {c['product']} so you can see it "
                "on your own page before deciding anything. Would that be useful?"
            ),
        }

    raise ValueError(f"mock has no answer for task '{task}'")
