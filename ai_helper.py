"""
ai_helper.py - title / description / tags / hashtags generator.

Uses Google's Gemini API through its plain REST endpoint (only the standard
library is needed - no extra pip package). Get a free key at
https://aistudio.google.com/apikey and paste it in the app's Settings tab
(or set the GEMINI_API_KEY environment variable).

If no key is set, or the request fails, a simple offline keyword-based
generator is used so the button always produces something.
"""
import json
import os
import re
import urllib.error
import urllib.request
from collections import Counter

DEFAULT_MODEL = "gemini-2.5-flash"   # editable in Settings if Google renames/retires models
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

STOPWORDS = set("""a about above after again all also am an and any are as at be because been before
being below between both but by can could did do does doing down during each few for from further had has
have having he her here hers him his how i if in into is it its just like me more most my no nor not now of
off on once only or other our out over own really same she should so some such than that the their them
then there these they this those through to too under until up very was we were what when where which while
who whom why will with would you your yours get got going gonna know thing things one two""".split())

PROMPT = """You write metadata for a YouTube Short.
Video title: {title}
Channel/topic notes from the creator: {notes}
Transcript of the clip (may be empty): {transcript}

Rules:
- Title: max 70 characters, curiosity-driven but HONEST - it must match what the clip actually shows. No fake claims.
- Description: 2-3 short sentences giving real context or the creator's own take, then one call to action.
- tags: 8-12 plain search keywords (no # sign).
- hashtags: 3-5 hashtags including #Shorts.
Return ONLY JSON: {{"title": str, "description": str, "tags": [str], "hashtags": [str]}}"""


def generate_metadata(title="", transcript="", notes="", api_key="", model=DEFAULT_MODEL,
                      log_cb=None):
    """Return {'title','description','tags'(list),'hashtags'(list),'source'}."""
    log = log_cb or (lambda m: None)
    api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
    if api_key:
        try:
            result = _ask_gemini(title, transcript, notes, api_key, model or DEFAULT_MODEL)
            result["source"] = "gemini"
            return result
        except Exception as e:
            log(f"Gemini failed ({e}); using offline generator instead.")
    else:
        log("No Gemini API key set - using the offline generator (add a key in Settings for better results).")
    result = offline_metadata(title, transcript, notes)
    result["source"] = "offline"
    return result


def _ask_gemini(title, transcript, notes, api_key, model):
    prompt = PROMPT.format(title=title or "(unknown)", notes=notes or "(none)",
                           transcript=(transcript or "")[:6000] or "(none)")
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.8, "responseMimeType": "application/json"},
    }).encode("utf-8")
    req = urllib.request.Request(
        ENDPOINT.format(model=model), data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:200]
        raise RuntimeError(f"HTTP {e.code}: {detail}")
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return normalize(json.loads(re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()))


def normalize(d):
    """Clean model output into the shape the GUI expects."""
    title = str(d.get("title", "")).strip()[:100]
    tags = [str(t).strip().lstrip("#") for t in d.get("tags", []) if str(t).strip()]
    hashtags = []
    for h in d.get("hashtags", []):
        h = "#" + re.sub(r"\W+", "", str(h))
        if len(h) > 1 and h.lower() not in [x.lower() for x in hashtags]:
            hashtags.append(h)
    if "#shorts" not in [h.lower() for h in hashtags]:
        hashtags.append("#Shorts")
    return {"title": title, "description": str(d.get("description", "")).strip(),
            "tags": tags[:15], "hashtags": hashtags[:6]}


def offline_metadata(title="", transcript="", notes=""):
    """Keyword-frequency fallback (no internet, no key)."""
    text = f"{title} {notes} {transcript}".lower()
    words = [w for w in re.findall(r"[a-z]{4,}", text) if w not in STOPWORDS]
    top = [w for w, _ in Counter(words).most_common(10)]
    base = (title or notes or "Must-see moment").strip()
    return normalize({
        "title": base[:70],
        "description": f"{base}. Follow for more and tell me what you think in the comments!",
        "tags": top,
        "hashtags": ["#" + w for w in top[:3]] + ["#Shorts"],
    })
