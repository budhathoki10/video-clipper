"""Read what is performing on YouTube and turn it into a trend profile.

Uses YouTube search through yt-dlp, so no API key is needed. The profile is
fed into clip selection and the audience panel.
"""
from __future__ import annotations

import re
import time
from typing import Any

import yt_dlp

from .. import db, llm

SYSTEM = """You analyse which videos and Shorts are performing on YouTube
and describe the patterns an editor should copy. Be specific and practical."""

PROMPT = """These are current top YouTube results for the searches: {queries}.
Each line has the view count, length in seconds, title and channel:

{rows}

Describe what is working. The editor makes short clips (15 to 90 seconds) cut
from longer videos, so give advice for those; ideal_length_sec must be the best
length for such a clip, not the length of the long videos listed. Return:
Keep every item short: this is shown as a scannable list, not prose.
{{"summary": "one sentence, at most 25 words",
  "hook_patterns": ["5 openings for the first 2 seconds, at most 8 words each"],
  "title_patterns": ["5 reusable title templates with [placeholders], at most 9 words each"],
  "topics": ["6 moment types getting views, at most 4 words each"],
  "ideal_length_sec": 0,
  "avoid": ["3 things that underperform, at most 8 words each"]}}"""


def search(query: str, limit: int) -> list[dict[str, Any]]:
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    rows = []
    for entry in info.get("entries") or []:
        if not entry or not entry.get("view_count"):
            continue
        rows.append({
            "id": entry.get("id"), "title": entry.get("title") or "",
            "channel": entry.get("channel") or entry.get("uploader") or "",
            "views": int(entry["view_count"]), "duration": int(entry.get("duration") or 0),
            "url": entry.get("url") or f"https://www.youtube.com/watch?v={entry.get('id')}",
            "query": query,
        })
    return rows


GENERAL = "trends"


def refresh(queries: list[str] | None = None, key: str = GENERAL) -> dict[str, Any]:
    """Read YouTube for `queries` (default: the searches in Settings) and
    store the result under `key`."""
    cfg = db.get_settings()["trends"]
    queries = [q.strip() for q in (queries or cfg["queries"]) if q.strip()]
    videos: dict[str, dict[str, Any]] = {}
    errors = []
    for query in queries:
        try:
            for row in search(query, int(cfg["per_query"])):
                videos.setdefault(row["id"], row)
        except Exception as exc:  # network or extractor failure for one query
            errors.append(f"{query}: {str(exc)[:120]}")
    ranked = sorted(videos.values(), key=lambda v: v["views"], reverse=True)
    if not ranked:
        raise RuntimeError("Could not read YouTube search results. " + "; ".join(errors[:2]))
    rows = "\n".join(
        f"- {v['views']:,} views | {v['duration']}s | {v['title']} ({v['channel']})"
        for v in ranked[:40]
    )
    profile: dict[str, Any] = {}
    warning = None
    try:
        data = llm.complete_json(SYSTEM, PROMPT.format(rows=rows, queries=", ".join(queries)))
        profile = data if isinstance(data, dict) else {}
    except llm.LLMError as exc:
        warning = str(exc)
    result = {"fetched_at": time.time(), "videos": ranked[:40], "profile": profile,
              "warning": warning, "queries": queries}
    db.kv_set(key, result)
    return result


def current(key: str = GENERAL) -> dict[str, Any] | None:
    return db.kv_get(key)


def is_stale(key: str = GENERAL) -> bool:
    data = current(key)
    hours = float(db.get_settings()["trends"]["refresh_hours"])
    return not data or time.time() - data.get("fetched_at", 0) > hours * 3600


def brief(key: str = GENERAL) -> str:
    """Compact text version of a profile for prompts."""
    data = current(key)
    profile = (data or {}).get("profile") or {}
    if not profile:
        return ""
    parts = [profile.get("summary", "")]
    for label, key in (("Hooks", "hook_patterns"), ("Topics", "topics"), ("Avoid", "avoid")):
        items = profile.get(key) or []
        if items:
            parts.append(f"{label}: " + "; ".join(str(i) for i in items[:5]))
    if profile.get("ideal_length_sec"):
        parts.append(f"Ideal length: about {profile['ideal_length_sec']} seconds")
    return "\n".join(p for p in parts if p)


def topic_queries(meta: dict[str, Any]) -> list[str]:
    """Searches that match one source video: its first tags and its category."""
    queries = [f"{tag} shorts" for tag in (meta.get("tags") or [])[:2] if 2 < len(tag) < 40]
    if meta.get("category"):
        queries.append(f"{meta['category']} shorts viral")
    return queries


def brief_for(meta: dict[str, Any]) -> str:
    """Trend advice for the video being clipped, read from YouTube when the
    stored profile is older than the refresh interval. Never raises: trends
    improve selection but are not required for it."""
    cfg = db.get_settings()["trends"]
    queries = topic_queries(meta) if cfg.get("match_topic", True) else []
    key = GENERAL
    if queries:
        slug = re.sub(r"[^a-z0-9]+", "-", " ".join(queries).lower()).strip("-")[:80]
        key = f"trends:{slug}"
    try:
        if is_stale(key):
            refresh(queries or None, key)
    except Exception:
        pass
    return brief(key) or brief(GENERAL)
