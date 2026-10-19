"""
checks.py - pre-upload checklist.

run_checks() returns [(level, message)] with level 'ok' | 'warn' | 'error'.
Any 'error' blocks the upload. These checks catch common mistakes and remind you
about rights; they cannot guarantee monetisation or copyright clearance.
"""
import datetime
import json
import os
import re

import analytics
import config
import processor

BGM_LICENSES = os.path.join(processor.BGM_DIR, "licenses.json")   # {"track.mp3": "CC0 - Pixabay"}


def parse_schedule(text):
    """'2026-10-25 18:30' (your local time) -> timezone-aware UTC datetime, or None if blank."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        local = datetime.datetime.strptime(text, "%Y-%m-%d %H:%M")
    except ValueError:
        raise ValueError("Schedule must look like 2026-10-25 18:30 (your local time).")
    return local.astimezone().astimezone(datetime.timezone.utc)   # naive -> local tz -> UTC


def _bgm_license(name):
    try:
        with open(BGM_LICENSES, encoding="utf-8") as f:
            return (json.load(f).get(name) or "").strip()
    except Exception:
        return ""


def run_checks(path, title, description, tags, rights, render_meta=None, source_url="",
               schedule_text="", privacy="private"):
    res = []
    add = lambda level, msg: res.append((level, msg))
    meta = render_meta or {}

    # ---- file
    if not path or not os.path.exists(path):
        add("error", "Video file not found.")
    else:
        mb = os.path.getsize(path) / 1_048_576
        dur = processor.get_duration(path) or 0
        w, h = processor.get_size(path)
        if not path.lower().endswith((".mp4", ".mov")):
            add("warn", "File is not .mp4/.mov - YouTube may re-encode it slowly.")
        if dur > 180:
            add("error", f"Duration {dur:.0f}s is over the 3-minute Shorts limit.")
        elif dur > 0:
            add("ok", f"Duration {dur:.0f}s (Shorts can be up to 3 minutes).")
        if w > h:
            add("warn", f"Video is {w}x{h} (landscape) - it will not be treated as a Short.")
        else:
            add("ok", f"Vertical/square format ({w}x{h}).")
        if not processor.has_audio(path):
            add("warn", "No audio track.")
        if mb > 500:
            add("warn", f"File is {mb:.0f} MB - unusually large for a Short.")

    # ---- text
    t = (title or "").strip()
    if not t:
        add("error", "Title is empty.")
    else:
        if len(t) > 100:
            add("error", f"Title is {len(t)} characters (max 100).")
        elif len(t) < 20:
            add("warn", "Title is very short - add the main keyword.")
        else:
            add("ok", f"Title length {len(t)}/100.")
        if "<" in t or ">" in t:
            add("error", "Title cannot contain < or >.")
        if t.isupper() and len(t) > 12:
            add("warn", "Title is ALL CAPS - can look spammy.")
    d = description or ""
    if len(d) > 5000:
        add("error", f"Description is {len(d)} characters (max 5000).")
    hashtags = re.findall(r"#\w+", d + " " + t)
    if len(hashtags) > 15:
        add("warn", "More than 15 hashtags - YouTube ignores all hashtags in that case.")
    if "#shorts" not in [x.lower() for x in hashtags]:
        add("warn", "No #Shorts hashtag.")
    tag_list = [x.strip() for x in (tags.split(",") if isinstance(tags, str) else tags) if x.strip()]
    if not tag_list:
        add("warn", "No tags set.")
    elif sum(len(x) + 1 for x in tag_list) > 500:
        add("error", "Tags exceed 500 characters.")

    # ---- schedule
    try:
        when = parse_schedule(schedule_text)
        if when:
            if when <= datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=5):
                add("error", "Scheduled time is in the past (or less than 5 minutes away).")
            else:
                add("ok", f"Scheduled for {when.astimezone():%Y-%m-%d %H:%M} local - stays private until then.")
        else:
            add("ok", f"Will be published as {privacy}.")
    except ValueError as e:
        add("error", str(e))

    # ---- rights, music, source
    if not rights:
        add("error", "Tick the rights confirmation (Studio tab) before uploading.")
    bgm = meta.get("bgm")
    if bgm:
        lic = _bgm_license(bgm)
        if lic:
            add("ok", f"Music '{bgm}' licence note: {lic}")
        else:
            add("warn", f"No licence note for music '{bgm}'. Add it to bgm/licenses.json and check the terms "
                        "(some free tracks need credit or forbid monetised use).")
    if source_url:
        add("warn", "This clip comes from a link. Make sure you have the owner's permission to reuse it "
                    "(or a licence such as Creative Commons) - editing alone does not create that right.")
        if not meta.get("voiceover") and not meta.get("cards"):
            add("warn", "Little original commentary was added (no voice-over or text cards) - "
                        "YouTube's reused-content policy looks for real added value.")
    # ---- duplicates
    if t and any((u.get("title") or "").strip().lower() == t.lower() for u in analytics.load_uploads()):
        add("warn", "You already uploaded a video with this exact title.")
    return res


def has_errors(results):
    return any(level == "error" for level, _ in results)
