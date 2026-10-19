"""
captions.py - word-level timing and animated captions.

Pipeline:  transcript segments -> words with timings -> chunks -> ASS subtitle
file -> burned in by FFmpeg's `ass` filter (libass), so rendering stays light.

Word timing sources (first one available wins):
  1. YouTube auto-captions (json3 has per-word offsets)   - links
  2. a .srt / .vtt file next to a local video            - times spread over each cue
  3. optional local transcription with faster-whisper     - pip install faster-whisper
"""
import os
import re
import subprocess
import sys
import tempfile

STYLES = ["Pop Highlight", "Karaoke Fill", "Bold Single Word", "Clean Subtitle"]
_NOWIN = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
PUNCT_END = (".", "?", "!", ",", ";", ":")


# --------------------------------------------------------------------------
# Words
# --------------------------------------------------------------------------
def words_from_segments(segments):
    """Segments ([{'start','end','text', optional 'words'}]) -> flat [{'text','start','end'}]."""
    words = []
    for seg in segments or []:
        if seg.get("words"):
            words.extend(dict(w) for w in seg["words"])
            continue
        tokens = seg["text"].split()
        if not tokens:
            continue
        total = max(seg["end"] - seg["start"], 0.2 * len(tokens))
        weights = [len(t) + 2 for t in tokens]           # longer words take longer to say
        t, wsum = seg["start"], sum(weights)
        for tok, wgt in zip(tokens, weights):
            d = total * wgt / wsum
            words.append({"text": tok, "start": t, "end": t + d})
            t += d
    # drop exact repeats caused by rolling auto-captions
    clean = []
    for w in words:
        if clean and w["text"] == clean[-1]["text"] and w["start"] < clean[-1]["end"] - 0.05:
            continue
        clean.append(w)
    for a, b in zip(clean, clean[1:]):                    # no overlaps
        if a["end"] > b["start"]:
            a["end"] = max(a["start"] + 0.05, b["start"])
    return clean


def clip_words(words, t0, t1):
    """Words inside [t0, t1], re-timed so the clip starts at 0."""
    t0 = t0 or 0.0
    out = []
    for w in words:
        if w["start"] >= t0 - 0.05 and (t1 is None or w["start"] < t1):
            end = min(w["end"], t1) if t1 is not None else w["end"]
            out.append({"text": w["text"], "start": max(w["start"] - t0, 0.0), "end": max(end - t0, 0.05)})
    return out


def parse_subtitle_file(path):
    """.srt / .vtt -> segments."""
    with open(path, encoding="utf-8-sig", errors="ignore") as f:
        raw = f.read().replace("\r", "")
    pat = re.compile(r"(?:(\d+):)?(\d+):(\d+)[,.](\d+)\s*-->\s*(?:(\d+):)?(\d+):(\d+)[,.](\d+)")

    def sec(h, m, s, ms):
        return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")[:3]) / 1000

    segs = []
    for block in re.split(r"\n\s*\n", raw):
        lines = block.strip().split("\n")
        for i, ln in enumerate(lines):
            m = pat.search(ln)
            if m:
                text = re.sub(r"<[^>]+>", "", " ".join(lines[i + 1:])).strip()
                if text:
                    g = m.groups()
                    segs.append({"start": sec(*g[:4]), "end": sec(*g[4:]), "text": text})
                break
    return segs


def find_sidecar_subtitles(video_path):
    stem = os.path.splitext(video_path)[0]
    for ext in (".srt", ".vtt", ".en.srt", ".en.vtt"):
        if os.path.exists(stem + ext):
            return stem + ext
    return None


def transcribe_local(path, t0=0.0, t1=None, model="base", language="en", log=None):
    """Transcribe only the clip range with faster-whisper. Times are returned in source time."""
    log = log or (lambda m: None)
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise RuntimeError("Local transcription needs: pip install faster-whisper (about 150 MB for the 'base' model).")
    import processor                                    # lazy: avoids a circular import
    fd, wav = tempfile.mkstemp(suffix=".wav", prefix="vas_tr_")
    os.close(fd)
    try:
        cmd = [processor.find_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-ss", str(t0 or 0), "-i", path]
        if t1:
            cmd += ["-t", str(t1 - (t0 or 0))]
        cmd += ["-vn", "-ac", "1", "-ar", "16000", wav]
        subprocess.run(cmd, check=True, creationflags=_NOWIN)
        log(f"Transcribing locally with Whisper '{model}' (CPU, int8)...")
        wm = WhisperModel(model, device="cpu", compute_type="int8")
        segs, _ = wm.transcribe(wav, language=language or None, word_timestamps=True, vad_filter=True)
        out = []
        for s in segs:
            words = [{"text": w.word.strip(), "start": w.start + (t0 or 0), "end": w.end + (t0 or 0)}
                     for w in (s.words or []) if w.word.strip()]
            out.append({"start": s.start + (t0 or 0), "end": s.end + (t0 or 0), "text": s.text.strip(), "words": words})
        return out
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass


# --------------------------------------------------------------------------
# ASS generation
# --------------------------------------------------------------------------
def _bgr(hex_rgb, alpha=None):
    """RGB hex -> ASS colour. alpha=None gives the inline-tag form &HBBGGRR&, else the style form &HAABBGGRR."""
    v = str(hex_rgb).lower().replace("0x", "").replace("#", "")
    r, g, b = v[0:2], v[2:4], v[4:6]
    return (f"&H{b}{g}{r}&" if alpha is None else f"&H{alpha:02X}{b}{g}{r}").upper()


def _ts(sec):
    sec = max(sec, 0)
    cs = int(round(sec * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _safe(text):
    return text.replace("{", "(").replace("}", ")").replace("\\", "").replace("\n", " ")


def chunk_words(words, max_words=3, max_chars=22, gap=0.6):
    """Group words into short on-screen phrases, breaking on punctuation / pauses."""
    chunks, cur = [], []
    for w in words:
        if cur and (len(cur) >= max_words or sum(len(x["text"]) + 1 for x in cur) + len(w["text"]) > max_chars
                    or w["start"] - cur[-1]["end"] > gap):
            chunks.append(cur)
            cur = []
        cur.append(w)
        if w["text"].endswith(PUNCT_END):
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


def emphasis_words(words):
    """Words worth colouring: numbers and hype words."""
    import analyzer
    out = set()
    for w in words:
        key = re.sub(r"[^a-z0-9%$']", "", w["text"].lower())
        if key and (re.search(r"\d", key) or key in analyzer.HYPE_WORDS):
            out.add(key)
    return out


def build_ass(words, style, accent_hex="0xE63946", bottom_y=1060, play=(720, 1280), font="Arial"):
    """Return the text of an .ass file for the given (clip-relative) words."""
    pw, ph = play
    margin_v = max(ph - bottom_y, 20)
    accent_s, white_s = _bgr(accent_hex, 0), "&H00FFFFFF"           # style-line colours
    accent, white, gold = _bgr(accent_hex), "&HFFFFFF&", "&H00D7FF&"  # inline-tag colours (gold = #FFD700)
    emph = emphasis_words(words)
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {pw}
PlayResY: {ph}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{font},64,{white_s},{white_s},&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,5,2,2,40,40,{margin_v},1
Style: Fill,{font},62,{accent_s},{white_s},&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,5,2,2,40,40,{margin_v},1
Style: Box,{font},52,{white_s},{white_s},&H99000000,&H00000000,-1,0,0,0,100,100,0,0,3,12,0,2,50,50,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []

    def add(start, end, sty, text):
        ev.append(f"Dialogue: 0,{_ts(start)},{_ts(max(end, start + 0.06))},{sty},,0,0,0,,{text}")

    def disp(w, upper):
        t = _safe(w["text"])
        return t.upper() if upper else t

    if style == "Karaoke Fill":
        for ch in chunk_words(words, 5, 30):
            parts = []
            for i, w in enumerate(ch):
                dur = (ch[i + 1]["start"] if i + 1 < len(ch) else w["end"]) - w["start"]
                parts.append(f"{{\\kf{max(int(round(dur * 100)), 1)}}}{disp(w, False)}")
            add(ch[0]["start"], ch[-1]["end"] + 0.15, "Fill", " ".join(parts))
    elif style == "Bold Single Word":
        y = bottom_y - 90
        for i, w in enumerate(words):
            end = words[i + 1]["start"] if i + 1 < len(words) and words[i + 1]["start"] - w["end"] < 0.4 else w["end"]
            key = re.sub(r"[^a-z0-9%$']", "", w["text"].lower())
            col = gold if key in emph else white
            add(w["start"], end, "Main",
                f"{{\\an5\\pos({pw // 2},{y})\\fs120\\1c{col}\\fscx60\\fscy60\\t(0,80,\\fscx100\\fscy100)}}{disp(w, True)}")
    elif style == "Clean Subtitle":
        for ch in chunk_words(words, 6, 34, 0.8):
            add(ch[0]["start"], ch[-1]["end"] + 0.1, "Box", " ".join(disp(w, False) for w in ch))
    else:  # Pop Highlight (default)
        chunks = chunk_words(words, 3, 22)
        for ci, ch in enumerate(chunks):
            nxt = chunks[ci + 1][0]["start"] if ci + 1 < len(chunks) else ch[-1]["end"] + 0.3
            chunk_end = min(ch[-1]["end"] + 0.2, nxt)
            for i, w in enumerate(ch):
                end = ch[i + 1]["start"] if i + 1 < len(ch) else chunk_end
                parts = []
                for j, x in enumerate(ch):
                    key = re.sub(r"[^a-z0-9%$']", "", x["text"].lower())
                    if j == i:
                        parts.append(f"{{\\1c{accent}\\fscx100\\fscy100\\t(0,90,\\fscx122\\fscy122)}}{disp(x, True)}{{\\r}}")
                    elif key in emph:
                        parts.append(f"{{\\1c{gold}}}{disp(x, True)}{{\\r}}")
                    else:
                        parts.append(disp(x, True))
                add(w["start"], end, "Main", " ".join(parts))
    return head + "\n".join(ev) + "\n"


def write_ass(words, style, accent_hex, bottom_y, directory=None):
    """Write the .ass to a temp file and return its path (caller deletes it)."""
    fd, path = tempfile.mkstemp(suffix=".ass", prefix="vas_cap_", dir=directory)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(build_ass(words, style, accent_hex, bottom_y))
    return path
