"""Build the ASS subtitle file burned into a clip: word-by-word captions with
the spoken word highlighted, the hook title, and the source credit."""
from __future__ import annotations

import re
from typing import Any

SIZE_FACTOR = {"small": 0.062, "medium": 0.078, "large": 0.096}
DEFAULT_FONT = "Poppins ExtraBold"  # bundled in backend/assets/fonts
POP = 114  # percent size of the spoken word at the moment it is said
SENTENCE_END = re.compile(r"[.!?…]$")
MAX_GAP = 0.6  # a longer pause starts a new caption group
TITLE_SECONDS = 4.0


def ass_color(hex_color: str) -> str:
    """#RRGGBB → ASS &H00BBGGRR."""
    value = hex_color.lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
        value = "FFFFFF"
    return f"&H00{value[4:6]}{value[2:4]}{value[0:2]}".upper()


def ass_time(seconds: float) -> str:
    cs = max(int(round(seconds * 100)), 0)
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def display(word: str, upper: bool) -> str:
    """How a spoken word is shown: no commas or full stops (they read as noise
    in short captions), question and exclamation marks kept."""
    shown = word.strip().strip("\"“”").rstrip(".,;:") or word.strip()
    return shown.upper() if upper else shown


def escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def group_words(words: list[dict[str, Any]], per_line: int) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for word in words:
        ends_sentence = bool(SENTENCE_END.search(word["w"]))
        gap = word["s"] - current[-1]["e"] if current else 0.0
        # A full line normally starts a new group, but the last word of a
        # sentence stays with its sentence rather than opening the next line.
        if current and (gap > MAX_GAP or (len(current) >= per_line and not ends_sentence)
                        or len(current) > per_line):
            groups.append(current)
            current = []
        current.append(word)
        if ends_sentence:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


MAX_LINE_CHARS = 200


def lines_for(words: list[dict[str, Any]], per_line: int) -> list[dict[str, Any]]:
    """The caption lines of a clip as they appear on screen, for editing: start, end and text."""
    return [{"s": round(group[0]["s"], 2), "e": round(group[-1]["e"], 2), "text": " ".join(w["w"] for w in group)}
            for group in group_words(words, max(int(per_line), 1))]


def words_from_lines(lines: list[dict[str, Any]], recognised: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Words with times from edited caption lines. A line keeps its recognised word times when it
    still has as many words (a spelling fix); otherwise its words share the line's time evenly.
    An emptied line shows nothing."""
    out: list[dict[str, Any]] = []
    for line in sorted(lines, key=lambda line: line["s"]):
        start, end = float(line["s"]), float(line["e"])
        tokens = str(line["text"]).split()
        if not tokens or end <= start:
            continue
        same = [w for w in recognised if w["s"] >= start - 0.01 and w["e"] <= end + 0.01]
        if len(same) == len(tokens):
            out += [{"w": t, "s": w["s"], "e": w["e"]} for t, w in zip(tokens, same)]
        else:
            step = (end - start) / len(tokens)
            out += [{"w": t, "s": round(start + i * step, 3), "e": round(start + (i + 1) * step, 3)}
                    for i, t in enumerate(tokens)]
    return out


def build_ass(*, words: list[dict[str, Any]], width: int, height: int, duration: float,
              captions: dict[str, Any], title: str = "", credit: str = "",
              title_all_clip: bool = False, title_top: float | None = None,
              caption_center: float | None = None) -> str:
    """`words` use clip-relative times in seconds. `title_top` (a share of the height) moves the hook
    title down, for example below a webcam panel so it does not cover the creator's face.
    `caption_center` (a share of the height) centres the captions on that line, for example the seam
    between two halves."""
    base = min(width, height)
    vertical = height > width
    cap_size = int(base * SIZE_FACTOR.get(captions.get("size", "medium"), 0.075))
    title_size = int(base * 0.056)
    credit_size = int(base * 0.030)
    primary = ass_color(captions.get("color", "#FFFFFF"))
    highlight = ass_color(captions.get("highlight", "#FFE600"))
    font = escape(captions.get("font") or DEFAULT_FONT).replace(",", " ")
    margin_x = int(width * 0.07)
    if captions.get("position") == "middle":
        cap_align, cap_margin = 5, 0
    else:
        cap_align, cap_margin = 2, int(height * (0.22 if vertical else 0.08))
    top = int(height * (0.10 if vertical else 0.07))
    credit_top = int(top * 0.55)
    if title_top is not None:
        top, credit_top = int(height * title_top), int(height * 0.015)

    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font},{cap_size},{primary},{primary},&H00000000,&H78000000,-1,0,0,0,100,100,0,0,1,{max(cap_size * 0.11, 2):.1f},{max(cap_size * 0.05, 1):.1f},{cap_align},{margin_x},{margin_x},{cap_margin},1
Style: Title,{DEFAULT_FONT},{title_size},&H00111111,&H00111111,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,{title_size * 0.28:.1f},0,8,{margin_x},{margin_x},{top},1
Style: Credit,Arial,{credit_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,0,0,1,{max(credit_size * 0.08, 1.5):.1f},0,8,{margin_x},{margin_x},{credit_top},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []

    def add(start: float, end: float, style: str, text: str) -> None:
        if end - start >= 0.02:
            events.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},{style},,0,0,0,,{text}")

    if credit:
        add(0, duration, "Credit", escape(credit))
    if title:
        add(0, duration if title_all_clip else min(TITLE_SECONDS, duration), "Title",
            "{\\fad(180,0)}" + escape(title))

    if captions.get("enabled", True):
        upper = captions.get("uppercase", True)
        groups = group_words(
            [w for w in words if w["e"] > 0 and w["s"] < duration],
            max(int(captions.get("words_per_line", 3)), 1),
        )
        for gi, group in enumerate(groups):
            next_start = groups[gi + 1][0]["s"] if gi + 1 < len(groups) else duration
            tokens = [escape(display(w["w"], upper)) for w in group]
            for wi, word in enumerate(group):
                start = max(word["s"], 0)
                if wi + 1 < len(group):
                    end = group[wi + 1]["s"]
                else:
                    end = min(word["e"] + 0.35, next_start)
                # The spoken word takes the highlight colour and pops: it starts
                # slightly enlarged and settles to normal size in 90 ms.
                parts = [
                    (f"{{\\c{highlight}&\\fscx{POP}\\fscy{POP}\\t(0,90,\\fscx100\\fscy100)}}{tok}"
                     f"{{\\c{primary}&\\fscx100\\fscy100}}") if i == wi else tok
                    for i, tok in enumerate(tokens)
                ]
                place = f"\\an5\\pos({width // 2},{int(height * caption_center)})" if caption_center is not None else ""
                add(start, min(end, duration), "Caption", "{" + place + "\\blur0.6}" + " ".join(parts))
    return head + "\n".join(events) + "\n"
