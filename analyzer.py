"""
analyzer.py - "smart cutting" helpers.

  Option A  get_chapters()      -> YouTube chapters read through yt-dlp
  Option B  find_hot_moments()  -> scans the transcript for high-energy windows
                                   (questions, exclamations, hype keywords,
                                   fast speech) and ranks them

fetch_video_info() does one metadata call (no video download) and returns the
title, chapters and transcript segments used by both options and by the AI
title generator.
"""
import json
import re
import urllib.request


# Words that tend to mark peaks of attention. Edit freely.
HYPE_WORDS = {
    "secret", "never", "always", "mistake", "wrong", "truth", "shocking", "insane",
    "crazy", "unbelievable", "incredible", "amazing", "wow", "finally", "biggest",
    "best", "worst", "free", "warning", "danger", "breaking", "exposed", "reveal",
    "revealed", "winner", "won", "lost", "challenge", "result", "results", "proof",
    "changed", "stop", "must", "need", "why", "how", "what", "tip", "trick", "hack",
    "million", "billion", "first", "last", "only", "impossible", "surprise",
}
QUESTION_STARTS = ("why", "how", "what", "who", "when", "which", "can", "do you", "did you",
                   "have you", "is it", "are you", "would you")


# --------------------------------------------------------------------------
# Metadata + transcript
# --------------------------------------------------------------------------
def _pick_caption_url(info, lang="en"):
    """Prefer human subtitles, then auto captions; prefer json3 format."""
    for source in ("subtitles", "automatic_captions"):
        tracks = info.get(source) or {}
        keys = [k for k in tracks if k == lang or k.startswith(lang + "-") or k.startswith(lang + "_")]
        if not keys and tracks and source == "subtitles":
            keys = [next(iter(tracks))]
        for k in keys:
            for fmt in tracks[k]:
                if fmt.get("ext") == "json3" and fmt.get("url"):
                    return fmt["url"]
    return None


def parse_json3(data):
    """
    YouTube json3 captions -> [{'start','end','text', optional 'words'}] (seconds).
    Auto-generated captions carry per-word offsets, which we keep for animated captions.
    """
    segs = []
    for ev in data.get("events", []):
        parts = ev.get("segs")
        if not parts or "tStartMs" not in ev:
            continue
        text = "".join(p.get("utf8", "") for p in parts).replace("\n", " ").strip()
        if not text:
            continue
        start = ev["tStartMs"] / 1000
        end = start + ev.get("dDurationMs", 2000) / 1000
        seg = {"start": start, "end": end, "text": text}
        words = [{"text": p["utf8"].strip(), "start": start + p.get("tOffsetMs", 0) / 1000, "end": 0.0}
                 for p in parts if p.get("utf8", "").strip()]
        if len(words) > 1 and any("tOffsetMs" in p for p in parts[1:]):
            for w, nxt in zip(words, words[1:]):
                w["end"] = max(nxt["start"], w["start"] + 0.05)
            words[-1]["end"] = max(end, words[-1]["start"] + 0.1)
            seg["words"] = words
        segs.append(seg)
    return segs


def fetch_video_info(url, lang="en"):
    """Return {'title','duration','uploader','chapters','segments'} for a URL."""
    from yt_dlp import YoutubeDL  # imported here so the rest of the module works without it
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    segments = []
    cap_url = _pick_caption_url(info, lang)
    if cap_url:
        try:
            with urllib.request.urlopen(cap_url, timeout=20) as r:
                segments = parse_json3(json.loads(r.read().decode("utf-8")))
        except Exception:
            segments = []   # transcript is optional - everything else still works
    return {
        "title": info.get("title", ""),
        "duration": info.get("duration") or 0,
        "uploader": info.get("uploader", ""),
        "thumbnail": info.get("thumbnail"),
        "chapters": get_chapters(info),
        "segments": segments,
    }


# --------------------------------------------------------------------------
# Option A - chapters
# --------------------------------------------------------------------------
def get_chapters(info):
    """[{'title','start','end'}] from yt-dlp's info dict (empty if none)."""
    return [{"title": c.get("title", f"Chapter {i + 1}"),
             "start": float(c.get("start_time", 0)),
             "end": float(c.get("end_time", 0))}
            for i, c in enumerate(info.get("chapters") or [])]


def chapter_to_clip(chapter, clip_len=45):
    """Chapter -> (start, end) capped at clip_len seconds."""
    start = chapter["start"]
    end = chapter["end"] or start + clip_len
    return start, min(end, start + clip_len)


# --------------------------------------------------------------------------
# Option B - transcript spike detection
# --------------------------------------------------------------------------
def _score(text, duration):
    words = re.findall(r"[a-zA-Z']+", text.lower())
    if not words:
        return 0.0, []
    reasons, score = [], 0.0
    hype = [w for w in words if w in HYPE_WORDS]
    if hype:
        score += min(len(hype), 8) * 1.5
        reasons.append("hype words: " + ", ".join(sorted(set(hype))[:4]))
    q = text.count("?")
    if q:
        score += min(q, 4) * 2.5
        reasons.append(f"{q} question(s)")
    sentences = re.split(r"(?<=[.!?])\s+", text)
    q_starts = sum(1 for s in sentences if s.lower().startswith(QUESTION_STARTS))
    if q_starts:
        score += q_starts * 1.5
    ex = text.count("!")
    if ex:
        score += min(ex, 4) * 2
        reasons.append("emphasis")
    nums = re.findall(r"\d[\d,\.]*", text)
    if nums:
        score += min(len(nums), 3)
        reasons.append("numbers")
    wps = len(words) / max(duration, 1)           # fast speech = energy
    if wps > 3.0:
        score += (wps - 3.0) * 2
        reasons.append("fast pace")
    return score, reasons


FILLER_STARTS = ("so ", "um ", "uh ", "hi ", "hello", "hey guys", "welcome", "today we", "okay so", "alright")
CONTRAST = {"but", "however", "suddenly", "actually", "instead", "except", "until", "yet", "though", "unexpectedly"}
RESOLUTION = ("that's why", "which means", "turns out", "the answer", "in the end", "and that's how",
              "so that's", "here's the thing", "the result", "the lesson", "that's the")
DANGLING = {"and", "but", "so", "because", "or", "then", "that", "which", "to", "of", "the", "a", "with"}


def build_sentences(segments, max_len=14.0, gap=0.8):
    """Group caption lines into sentence-like units (punctuation, pauses or max length end a unit)."""
    sents, cur = [], None
    for s in segments:
        if cur and (s["start"] - cur["end"] > gap or cur["text"].rstrip().endswith((".", "?", "!"))
                    or s["end"] - cur["start"] > max_len):
            sents.append(cur)
            cur = None
        if cur is None:
            cur = {"start": s["start"], "end": s["end"], "text": s["text"]}
        else:
            cur["end"] = s["end"]
            cur["text"] += " " + s["text"]
    if cur:
        sents.append(cur)
    return sents


def _hook(text):
    low = text.lower().strip()
    words = re.findall(r"[a-z']+", low)
    pts, why = 0.0, []
    if low.startswith(FILLER_STARTS):
        pts -= 3
        why.append("weak opening")
    if "?" in text or (words and words[0] in ("why", "how", "what", "who", "when")):
        pts += 3
        why.append("question hook")
    if re.search(r"\d", text):
        pts += 1.5
    if any(w in HYPE_WORDS for w in words[:12]):
        pts += 2
        why.append("bold opening")
    return pts, why


def _surprise(text):
    words = re.findall(r"[a-z']+", text.lower())
    n = sum(1 for w in words if w in CONTRAST)
    pts = min(n, 3) * 1.5 + min(text.count("!"), 3)
    return pts, (["surprise / turn"] if pts >= 1.5 else [])


def _ending(text):
    low = text.lower().strip()
    words = re.findall(r"[a-z']+", low)
    pts, why = 0.0, []
    if any(r in low for r in RESOLUTION):
        pts += 3
        why.append("payoff line")
    if low.endswith((".", "!", "?")):
        pts += 1.5
    if words and words[-1] in DANGLING:
        pts -= 3
        why.append("cuts mid-thought")
    if 0 < len(words) <= 14:
        pts += 1
    return pts, why


def find_hot_moments(segments, clip_len=45, top_n=5, step=5, min_score=3.0):
    """
    Story-aware clip finder. Candidate clips start and end on sentence boundaries and are scored on
      energy   - hype words, questions, numbers, speaking pace
      hook     - does the first sentence grab attention (and not start with filler)?
      surprise - contrast / reveal words
      ending   - does it land on a complete thought or payoff line?
    Returns top_n non-overlapping clips:
        [{'start','end','score','reason','text','parts'}]   (step is kept for backwards compatibility)
    """
    sents = build_sentences(segments)
    if not sents:
        return []
    cands = []
    for i, first in enumerate(sents):
        j, end = i, first["end"]
        while j + 1 < len(sents) and end - first["start"] < clip_len * 0.75:
            j += 1
            end = sents[j]["end"]
        if end - first["start"] > clip_len * 1.15 and j > i:
            j -= 1
            end = sents[j]["end"]
        if end - first["start"] < min(10, clip_len * 0.4):
            continue
        text = " ".join(x["text"] for x in sents[i:j + 1])
        energy, e_why = _score(text, end - first["start"])
        hook, h_why = _hook(first["text"])
        surp, s_why = _surprise(text)
        endp, n_why = _ending(sents[j]["text"])
        total = energy * 0.5 + hook + surp + endp
        if total >= min_score:
            cands.append({"start": first["start"], "end": end, "score": round(total, 1),
                          "reason": "; ".join(h_why + s_why + n_why + e_why[:2]) or "high energy",
                          "text": text[:160],
                          "parts": {"energy": round(energy * 0.5, 1), "hook": hook, "surprise": surp, "ending": endp}})
    cands.sort(key=lambda c: c["score"], reverse=True)
    picked = []
    for c in cands:
        if all(c["end"] <= p["start"] or c["start"] >= p["end"] for p in picked):
            picked.append(c)
        if len(picked) >= top_n:
            break
    return sorted(picked, key=lambda c: c["start"])


def apply_ai_scores(moments, ai_scores):
    """Blend model ratings (index -> 0..30) into the heuristic scores and re-sort by score."""
    for i, m in enumerate(moments):
        if i in ai_scores:
            m["score"] = round(m["score"] * 0.5 + ai_scores[i] * 0.5, 1)
            m["reason"] += f"; AI {ai_scores[i]:.0f}/30"
    return sorted(moments, key=lambda c: c["score"], reverse=True)


def slice_transcript(segments, start=None, end=None):
    """Transcript text between start and end seconds (None = whole video)."""
    lo, hi = start or 0, end if end is not None else float("inf")
    return " ".join(s["text"] for s in segments if lo <= s["start"] < hi)


def fmt_time(seconds):
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"
