"""Simulated audience panel.

Up to 100 viewer personas with different tastes react to a clip. Their answers
are combined into a 0-100 score. This is a prediction aid, not a measurement:
the personas only read the clip's transcript and metadata.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .. import llm

ARCHETYPES = [
    ("Casual scroller", "swipes away within 2 seconds unless something happens"),
    ("Fan of the creator", "follows this creator and enjoys their personality"),
    ("Topic expert", "knows this subject deeply and hates recycled or wrong takes"),
    ("Newcomer to the topic", "knows nothing about the subject, needs it to make sense alone"),
    ("Meme hunter", "shares anything that works as a reaction meme"),
    ("Group-chat sharer", "sends clips to friends when they are funny or shocking"),
    ("Skeptic", "thinks most Shorts are low effort and says so"),
    ("Younger teen", "13-15, watches Shorts for hours, loves fast edits"),
    ("Late-night viewer", "tired, wants something easy and entertaining"),
    ("Story lover", "wants a setup, tension and a payoff"),
    ("Detail spotter", "notices small things in the picture and background"),
    ("Clip channel regular", "has seen thousands of similar clips already"),
    ("Busy parent", "30s, 10 minutes of free time, low patience"),
    ("Hype chaser", "watches for big reactions, wins and chaos"),
    ("Learner", "stays when a clip teaches or reveals something new"),
    ("Sound-off viewer", "watches muted and relies on captions and visuals"),
    ("Non-native English viewer", "follows visuals more than speech"),
    ("Commenter", "watches mainly to argue or joke in the comments"),
    ("Trend follower", "only cares about what everyone is talking about this week"),
    ("Binge watcher", "lets Shorts autoplay and rarely swipes unless bored"),
]
MOODS = ["in a good mood", "bored", "distracted", "curious", "hard to impress"]
BATCH = 25

SYSTEM = """You simulate real YouTube Shorts viewers. Each persona reacts
independently and honestly. Most Shorts get swiped away: do not be generous.
A weak or confusing first 2 seconds means a low watch percentage."""

PROMPT = """A Short appears in the feed.

Title: {title}
On-screen hook text: {hook}
Length: {duration:.0f} seconds
Source: {channel}
What is said in the clip:
{transcript}

Trends right now:
{trends}

For each persona, return how they react.
- watch: percent of the clip they watch (0-100)
- like: true or false
- share: true or false
- comment: a short comment they would leave, or "" for none

Personas:
{personas}

Return: {{"reactions": [{{"id": 1, "watch": 0, "like": false, "share": false, "comment": ""}}]}}"""


def personas(count: int) -> list[dict[str, Any]]:
    out = []
    for i in range(max(min(count, 100), 0)):
        name, taste = ARCHETYPES[i % len(ARCHETYPES)]
        mood = MOODS[(i // len(ARCHETYPES)) % len(MOODS)]
        out.append({"id": i + 1, "type": name, "desc": f"{name}: {taste}; currently {mood}"})
    return out


def _react(batch: list[dict[str, Any]], context: dict[str, Any]) -> list[dict[str, Any]]:
    listing = "\n".join(f"{p['id']}. {p['desc']}" for p in batch)
    try:
        data = llm.complete_json(SYSTEM, PROMPT.format(personas=listing, **context),
                                 temperature=0.8, max_tokens=8000)
    except llm.LLMError:
        return []
    rows = data.get("reactions", []) if isinstance(data, dict) else data
    by_id = {p["id"]: p for p in batch}
    out = []
    for row in rows if isinstance(rows, list) else []:
        try:
            persona = by_id[int(row["id"])]
            out.append({
                "id": persona["id"], "type": persona["type"],
                "watch": max(0.0, min(float(row.get("watch") or 0), 100.0)),
                "like": bool(row.get("like")), "share": bool(row.get("share")),
                "comment": str(row.get("comment") or "")[:160],
            })
        except (KeyError, TypeError, ValueError):
            continue
    return out


def run_panel(*, title: str, hook: str, transcript: str, duration: float, channel: str,
              trends: str, size: int) -> dict[str, Any] | None:
    """Returns the panel summary, or None when no persona answered."""
    people = personas(size)
    if not people:
        return None
    context = {"title": title, "hook": hook or "(none)", "duration": duration,
               "channel": channel or "unknown", "transcript": transcript[:3500] or "(no speech)",
               "trends": trends or "No trend data yet."}
    batches = [people[i:i + BATCH] for i in range(0, len(people), BATCH)]
    reactions: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for result in pool.map(lambda b: _react(b, context), batches):
            reactions.extend(result)
    if not reactions:
        return None
    n = len(reactions)
    watch = sum(r["watch"] for r in reactions) / n
    like = sum(r["like"] for r in reactions) / n * 100
    share = sum(r["share"] for r in reactions) / n * 100
    finished = sum(r["watch"] >= 90 for r in reactions) / n * 100
    score = 0.45 * watch + 0.20 * finished + 0.15 * like + 0.20 * share

    groups: dict[str, list[float]] = {}
    for r in reactions:
        groups.setdefault(r["type"], []).append(r["watch"])
    by_type = sorted(
        ({"type": t, "watch": round(sum(v) / len(v))} for t, v in groups.items()),
        key=lambda g: g["watch"], reverse=True,
    )
    comments = [{"type": r["type"], "text": r["comment"], "watch": round(r["watch"])}
                for r in sorted(reactions, key=lambda r: r["watch"], reverse=True) if r["comment"]]
    return {
        "score": round(score, 1), "viewers": n, "watch": round(watch, 1),
        "finished": round(finished, 1), "like": round(like, 1), "share": round(share, 1),
        "by_type": by_type, "comments": comments[:4] + comments[-4:] if len(comments) > 8 else comments,
    }
