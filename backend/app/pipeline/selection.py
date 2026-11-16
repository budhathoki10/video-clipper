"""Choose the best moments of a video and where to cut them.

Three passes:
1. Content: the AI reads the whole transcript, section by section, and proposes
   the strongest moments from what is actually said.
2. Signals: the AI also judges the windows around audio peaks (loud reactions,
   sudden jumps), which catch moments that read flat on paper.
3. Ranking: the best candidates from both passes are judged side by side in one
   call, so scores are comparable across the whole video.
"""
from __future__ import annotations

import bisect
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .. import llm

PEAK_BATCH = 4  # audio-peak windows per AI call
LEAD, TAIL = 70.0, 45.0  # transcript shown before / after each audio peak
SECTION_SEC = 420.0  # the content pass reads the transcript in sections this long
SECTION_OVERLAP = 30.0
PER_SECTION = 3
EXCERPT_CHARS = 700

HOOK_RULES = """The hook is the line shown on screen over the first seconds. At most 6 words.
It must make a stranger who is scrolling past need to see what happens next:
- Open a curiosity gap: hint at the stakes or the outcome without giving the payoff away.
- Be specific to this moment (a number, a consequence, who is involved). Never generic
  ("Funny moment", "Watch this", "Insane clip").
- Plain, punchy words a teenager would say out loud. One emotion: shock, tension, humour, disbelief.
- Never promise something the clip does not deliver, and never just repeat the title.
- Style examples from other topics: "He Had 10 Seconds Left", "Nobody Expected This Answer",
  "Wait For The Last Pass", "This Cost Him Everything", "Why Did Everyone Go Silent?"."""

HOOKS_PROMPT = """Clip title: "{title}"
What is said in the clip:
{spoken}

What is working on YouTube right now:
{trends}

{hook_rules}

Write {count} different hooks for this clip, each using a different angle (a question, a
stake, a reaction, a number, a warning). Return: {{"hooks": ["", ""]}}"""

SYSTEM = """You are a senior short-form video editor. You work with any kind of
source: gaming, podcasts, interviews, sports, vlogs, tutorials, comedy, news.
You pick moments from long videos and streams that will hold a YouTube Shorts
viewer from the first second: a clear hook, rising tension or a joke, and a
payoff. You never pick a moment that needs earlier context to make sense."""

PROMPT = """Source video: "{title}" by {channel}.

What is working on YouTube right now:
{trends}

Below are {count} windows of the transcript. Each line starts with its time in
seconds. Return the moments worth posting: at most {per_window} per window, and
none for a window with nothing strong. Prefer one excellent clip over several
average ones.

A strong clip:
- opens on a line that makes sense with no earlier context and creates a question
- builds (stakes, conflict, a setup) and ends right after the payoff or reaction
- is about one thing only

Rules:
- start and end are seconds taken from the line times in that window.
- Length between {min_sec} and {max_sec} seconds.
- title: a YouTube title under 70 characters, no clickbait lies, no emoji.
- hook: {hook_rules}
- description: one or two plain sentences.
- hashtags: 3 to 5 lowercase tags without the # sign, specific to what happens
  in this clip, its topic and its creator (never generic words like "video").
- score: 0-100, your honest estimate of how well it would perform as a Short.
- reason: one sentence on why it works.

Return: {{"clips": [{{"window": 1, "start": 0, "end": 0, "title": "", "hook": "",
"description": "", "hashtags": [], "score": 0, "reason": ""}}]}}

{windows}"""

RANK_PROMPT = """Source video: "{title}" by {channel}.

These candidate clips were picked from different parts of the same video. Judge
them against each other and score each 0-100 as a YouTube Short. Be strict:
reserve 85+ for a clip you would post first. Mark down clips that need earlier
context, start slowly, end without a payoff, or repeat another candidate.
Also rewrite each hook if you can make it stronger, using these rules:
{hook_rules}

What is working on YouTube right now:
{trends}

{candidates}

Return: {{"ranking": [{{"id": 1, "score": 0, "reason": "one sentence", "hook": "improved hook"}}]}}"""


def clean_hook(value: Any) -> str:
    """A hook as shown on screen: no quotes, at most 7 words, or empty."""
    text = " ".join(str(value or "").replace("\n", " ").strip().strip("\"'“”").split())
    return text if 0 < len(text.split()) <= 7 else ""


def suggest_hooks(*, title: str, spoken: str, trends: str = "", count: int = 5) -> list[str]:
    """Several alternative hooks for one clip, best angles first."""
    data = llm.complete_json(SYSTEM, HOOKS_PROMPT.format(
        title=title, spoken=spoken[:3500] or "(no speech)", trends=trends or "No trend data yet.",
        count=count, hook_rules=HOOK_RULES), temperature=0.9)
    rows = data.get("hooks", []) if isinstance(data, dict) else data
    seen: set[str] = set()
    out: list[str] = []
    for row in rows if isinstance(rows, list) else []:
        hook = clean_hook(row)
        if hook and hook.lower() not in seen:
            seen.add(hook.lower())
            out.append(hook)
    return out[:count]


def _window_text(segments: list[dict[str, Any]], lo: float, hi: float) -> str:
    return "\n".join(f"[{s['s']:.1f}] {s['text']}" for s in segments if s["e"] >= lo and s["s"] <= hi)


def snap(words: list[dict[str, Any]], start: float, end: float) -> tuple[float, float]:
    """Move the cut points onto word boundaries so no word is clipped."""
    if not words:
        return start, end
    starts = [w["s"] for w in words]
    i = min(max(bisect.bisect_right(starts, start + 0.25) - 1, 0), len(words) - 1)
    j = min(max(bisect.bisect_right(starts, end) - 1, i), len(words) - 1)
    return max(words[i]["s"] - 0.15, 0.0), words[j]["e"] + 0.30


def _fit(start: float, end: float, min_sec: float, max_sec: float, total: float) -> tuple[float, float]:
    if end - start > max_sec:
        end = start + max_sec
    if end - start < min_sec:
        end = min(start + min_sec, total or start + min_sec)
        start = max(end - min_sec, 0.0)
    return start, end


def _peak_windows(cands: list[dict[str, float]]) -> list[dict[str, Any]]:
    return [{"lo": c["peak"] - LEAD, "hi": c["peak"] + TAIL,
             "note": f"audio peak near {c['peak']:.0f}s"} for c in cands]


def _sections(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not segments:
        return []
    end = segments[-1]["e"]
    out, lo = [], max(segments[0]["s"] - 1, 0.0)
    while lo < end:
        out.append({"lo": lo, "hi": lo + SECTION_SEC, "note": "full section"})
        lo += SECTION_SEC - SECTION_OVERLAP
    return out


def _ask(windows: list[dict[str, Any]], per_window: int, segments: list[dict[str, Any]],
         meta: dict[str, Any], clip_cfg: dict[str, Any], trends: str) -> list[dict[str, Any]]:
    blocks = []
    for n, window in enumerate(windows, 1):
        text = _window_text(segments, window["lo"], window["hi"])
        if text:
            blocks.append(f"--- Window {n} ({window['note']}) ---\n{text}")
    if not blocks:
        return []
    data = llm.complete_json(SYSTEM, PROMPT.format(
        title=meta.get("title", ""), channel=meta.get("channel", "") or "unknown",
        trends=trends or "No trend data yet.", count=len(blocks), per_window=per_window,
        min_sec=clip_cfg["min_sec"], max_sec=clip_cfg["max_sec"], windows="\n\n".join(blocks),
        hook_rules=HOOK_RULES,
    ), max_tokens=8000)
    clips = data.get("clips", []) if isinstance(data, dict) else data
    out = []
    for clip in clips if isinstance(clips, list) else []:
        try:
            index = int(clip.get("window", 1)) - 1
            window = windows[min(max(index, 0), len(windows) - 1)]
            start, end = float(clip["start"]), float(clip["end"])
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        if not (window["lo"] - 5 <= start < end <= window["hi"] + clip_cfg["max_sec"]):
            continue
        out.append({
            "start": start, "end": end,
            "title": str(clip.get("title") or meta.get("title", ""))[:100],
            "hook": str(clip.get("hook") or "")[:60],
            "description": str(clip.get("description") or ""),
            "hashtags": [str(t).lstrip("#").lower() for t in (clip.get("hashtags") or [])][:6],
            "score": max(0.0, min(float(clip.get("score") or 0), 100.0)),
            "reason": str(clip.get("reason") or ""),
        })
    return out


def _fallback(cands: list[dict[str, float]], meta: dict[str, Any],
              clip_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """No AI available: cut around the strongest audio peaks."""
    length = (clip_cfg["min_sec"] + clip_cfg["max_sec"]) / 2
    ranked = sorted(cands, key=lambda c: c["signal"], reverse=True)
    return [{
        "start": max(c["peak"] - length * 0.6, 0), "end": max(c["peak"] - length * 0.6, 0) + length,
        "title": meta.get("title", "Clip")[:100], "hook": "", "description": "",
        "hashtags": [], "score": 0.0, "reason": "Picked from audio peaks; the AI was unavailable.",
    } for c in ranked]


def _signal_bonus(signal: Any, start: float, end: float) -> float:
    """Up to 6 points for a clip that also stands out in the audio."""
    if signal is None or len(signal) == 0:
        return 0.0
    part = signal[int(start):max(int(end), int(start) + 1)]
    return 0.0 if len(part) == 0 else max(0.0, min(float(max(part)), 3.0)) * 2


def _rank(clips: list[dict[str, Any]], words: list[dict[str, Any]], meta: dict[str, Any],
          trends: str) -> None:
    """Re-score the candidates side by side. Leaves scores untouched on failure."""
    blocks = []
    for n, clip in enumerate(clips, 1):
        spoken = " ".join(w["w"] for w in words if clip["start"] <= w["s"] <= clip["end"])
        blocks.append(f"Candidate {n} ({clip['end'] - clip['start']:.0f}s, starts at "
                      f"{clip['start']:.0f}s)\nTitle: {clip['title']}\nHook: {clip['hook']}\n"
                      f"Said: {spoken[:EXCERPT_CHARS]}")
    try:
        data = llm.complete_json(SYSTEM, RANK_PROMPT.format(
            title=meta.get("title", ""), channel=meta.get("channel", "") or "unknown",
            trends=trends or "No trend data yet.", candidates="\n\n".join(blocks),
            hook_rules=HOOK_RULES))
    except llm.LLMError:
        return
    rows = data.get("ranking", []) if isinstance(data, dict) else data
    for row in rows if isinstance(rows, list) else []:
        try:
            clip = clips[int(row["id"]) - 1]
            clip["score"] = max(0.0, min(float(row["score"]), 100.0))
            if row.get("reason"):
                clip["reason"] = str(row["reason"])
            better = clean_hook(row.get("hook"))
            if better:
                clip["hook"] = better
        except (IndexError, KeyError, TypeError, ValueError):
            continue


def select_clips(*, cands: list[dict[str, float]], transcript: dict[str, Any],
                 meta: dict[str, Any], clip_cfg: dict[str, Any], trends: str = "",
                 signal: Any = None,
                 exclude: list[tuple[float, float]] | None = None) -> tuple[list[dict[str, Any]], str | None]:
    """Returns (clips sorted best first, warning or None).

    `exclude` lists (start, end) ranges already used by earlier clips of the same
    video: nothing overlapping them is returned, so a second pass finds new moments."""
    segments, words = transcript["segments"], transcript["words"]
    total = float(meta.get("duration") or 0)
    used = list(exclude or [])
    cands = [c for c in cands if not any(s - 5 <= c["peak"] <= e + 5 for s, e in used)]
    per_section = PER_SECTION + (2 if used else 0)  # the best moments are taken, so look a little deeper
    peaks = _peak_windows(cands)
    jobs: list[tuple[list[dict[str, Any]], int]] = [
        (peaks[i:i + PEAK_BATCH], 1) for i in range(0, len(peaks), PEAK_BATCH)]
    # A short source (a live window) is one section the peak pass already covers.
    if segments and segments[-1]["e"] - segments[0]["s"] > LEAD + TAIL + 30:
        jobs += [([section], per_section) for section in _sections(segments)]

    errors: list[str] = []

    def work(job: tuple[list[dict[str, Any]], int]) -> list[dict[str, Any]]:
        try:
            return _ask(job[0], job[1], segments, meta, clip_cfg, trends)
        except llm.LLMError as exc:
            errors.append(str(exc))
            return []

    picked: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        for result in pool.map(work, jobs):
            picked.extend(result)
    warning = None
    if not picked:
        warning = errors[0] if errors else None
        picked = _fallback(cands, meta, clip_cfg)

    # Clean cut points, then drop overlaps keeping the better clip.
    unique: list[dict[str, Any]] = []
    for clip in sorted(picked, key=lambda c: c["score"], reverse=True):
        start, end = snap(words, clip["start"], clip["end"])
        start, end = _fit(start, end, clip_cfg["min_sec"], clip_cfg["max_sec"], total)
        if any(start < o["end"] and end > o["start"] for o in unique):
            continue
        if any(start < e and end > s for s, e in used):
            continue
        unique.append({**clip, "start": round(start, 2), "end": round(end, 2)})

    shortlist = unique[:max(int(clip_cfg["max_clips"]) * 2, 6)]
    if len(shortlist) > 1 and not warning:
        _rank(shortlist, words, meta, trends)
    for clip in shortlist:
        clip["score"] = round(min(clip["score"] + _signal_bonus(signal, clip["start"], clip["end"]), 100.0), 1)

    final = [c for c in sorted(shortlist, key=lambda c: c["score"], reverse=True)
             if warning or c["score"] >= clip_cfg.get("min_score", 0)]
    return final[:int(clip_cfg["max_clips"])], warning
