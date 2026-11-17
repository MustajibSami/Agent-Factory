# How we work in this repo

1. Before changing code, read the files involved and say what you found. Do not edit yet.
2. Write a plan: files to change, and the exact check that proves it works. Wait for an OK on big changes.
3. Build one step at a time. Run `python -m pytest` after each step.
4. Never say something works unless you ran it. Show the output.
5. If a check fails, find the cause. Do not weaken or skip a test.
6. When finished, list what changed, what you verified, and what you did not verify.

# Project rules

- Rooms never call each other. They only move job cards through statuses in `factory/db.py`.
- Every model call goes through `Room.ask()` so the budget and cost meters stay correct.
- Anything public (posts, emails) must pass through the `awaiting_you` status first.
- Keep test mode working with no API keys and no network.

# Lessons learned
(add one line each time a mistake is corrected)
