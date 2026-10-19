"""
ai_tasks.py - task-specific prompts on top of ai_brain.Brain, each with an
offline fallback so the app is still useful with no key and no internet.

Tasks: metadata (SEO), commentary, rank_moments, trend_summary
Every function returns plain dicts/strings plus a 'source' naming the provider
that produced the result ('offline' when no AI was reachable).
"""
import re
from collections import Counter

from ai_brain import AIUnavailable

SYSTEM = ("You help a solo creator make honest, original YouTube Shorts. Never invent facts that are "
          "not supported by the material you are given. No clickbait that misrepresents the video. "
          "Reply ONLY with the JSON requested.")

STOPWORDS = set("""a about above after again all also am an and any are as at be because been before being
below between both but by can could did do does doing down during each few for from further had has have
having he her here hers him his how i if in into is it its just like me more most my no nor not now of off
on once only or other our out over own really same she should so some such than that the their them then
there these they this those through to too under until up very was we were what when where which while who
whom why will with would you your yours get got going gonna know thing things one two yeah okay right""".split())


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def top_keywords(text, n=10):
    words = [w for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in STOPWORDS]
    return [w for w, _ in Counter(words).most_common(n)]


def score_title(t):
    """Rough SEO/retention score (0-100) used to sort title options."""
    s, n = 50, len(t)
    s += 15 if 35 <= n <= 70 else (5 if n <= 90 else -15)
    s += 8 if re.search(r"\d", t) else 0
    s += 6 if "?" in t else 0
    s -= 15 if t.isupper() else 0
    s -= 10 if t.count("!") > 1 else 0
    s -= 8 if len(t.split()) < 3 else 0
    return max(0, min(100, s))


def _clean_tags(tags):
    out = []
    for t in tags or []:
        t = str(t).strip().lstrip("#")
        if t and t.lower() not in [x.lower() for x in out]:
            out.append(t)
    return out[:15]


def _hashtags(items):
    out = []
    for h in items or []:
        h = "#" + re.sub(r"\W+", "", str(h))
        if len(h) > 1 and h.lower() not in [x.lower() for x in out]:
            out.append(h)
    if "#shorts" not in [h.lower() for h in out]:
        out.append("#Shorts")
    return out[:6]


# --------------------------------------------------------------------------
# metadata / SEO
# --------------------------------------------------------------------------
def metadata(brain, title="", transcript="", notes="", keywords=None, hints="", log=None):
    """-> dict(title_options, description, tags, hashtags, thumbnail_ideas, source)"""
    keywords = keywords or []
    prompt = f"""Source video title: {title or '(unknown)'}
Creator notes: {notes or '(none)'}
Transcript of the clip (may be empty): {(transcript or '')[:5000] or '(none)'}
Search keyword ideas (from YouTube autocomplete): {', '.join(keywords[:15]) or '(none)'}
Performance hints from the creator's past Shorts: {hints or '(none yet)'}

Return JSON: {{"title_options": [5 different titles, each max 70 chars, honest, keyword near the front],
"description": "first line contains the main keyword; 2-3 short sentences of real context; one call to action",
"tags": [8-12 search keywords, no # sign], "hashtags": [3-5 hashtags including #Shorts],
"thumbnail_ideas": [3 short concepts: which moment to freeze + max 4 words of overlay text]}}"""
    try:
        d, src = brain.complete_json(prompt, SYSTEM, 0.8, log)
        titles = [str(t).strip()[:100] for t in d.get("title_options") or [d.get("title", "")] if str(t).strip()]
        if not titles:
            raise ValueError("no titles returned")
        return {"title_options": sorted(titles, key=score_title, reverse=True),
                "description": str(d.get("description", "")).strip(),
                "tags": _clean_tags(d.get("tags")), "hashtags": _hashtags(d.get("hashtags")),
                "thumbnail_ideas": [str(x) for x in (d.get("thumbnail_ideas") or [])][:4], "source": src}
    except (AIUnavailable, ValueError, KeyError, TypeError) as e:
        if log:
            log(f"AI unavailable ({e}); using the offline generator.")
        return offline_metadata(title, transcript, notes, keywords)


def offline_metadata(title="", transcript="", notes="", keywords=None):
    kws = (keywords or [])[:3] or top_keywords(f"{title} {notes} {transcript}", 5)
    base = (title or notes or "Must-see moment").strip()
    main = kws[0] if kws else base.split(" ")[0]
    options = [base[:70], f"{main.title()} explained in under a minute"[:70],
               f"The key moment about {main}"[:70], f"What to know about {main} (quick clip)"[:70],
               f"Why {main} matters"[:70]]
    tags = _clean_tags(list(kws) + top_keywords(f"{title} {notes} {transcript}", 8))
    return {"title_options": sorted(dict.fromkeys(options), key=score_title, reverse=True),
            "description": f"{main.title()} - {base}. Follow for more clips like this and tell me what you think in the comments!",
            "tags": tags, "hashtags": _hashtags(tags[:3]),
            "thumbnail_ideas": [f"Freeze the most expressive frame; overlay: \"{main.upper()}\"",
                                f"Split frame: before/after; overlay: \"{main.title()}?\"",
                                "Close-up on the key object with a 3-word label"],
            "source": "offline"}


# --------------------------------------------------------------------------
# commentary
# --------------------------------------------------------------------------
def commentary(brain, title, transcript, seconds, tone="Conversational", log=None):
    """Draft first-person commentary that fits `seconds`. Returns (text, source)."""
    words = max(15, int(seconds * 2.4))
    prompt = f"""Video title: {title or '(unknown)'}
Clip transcript: {(transcript or '')[:4000] or '(none)'}
Write about {words} words of {tone.lower()} first-person commentary a creator could speak over this clip:
add context, a reaction or a takeaway - do NOT just repeat the transcript, do not invent facts, no medical/financial claims.
Return JSON: {{"commentary": "..."}}"""
    try:
        d, src = brain.complete_json(prompt, SYSTEM, 0.9, log)
        return str(d["commentary"]).strip(), src
    except (AIUnavailable, ValueError, KeyError, TypeError):
        kws = top_keywords(transcript or title, 3)
        topic = ", ".join(kws) or "this"
        return (f"Here's what stood out to me about {topic}. Watch the moment closely - "
                f"it says a lot about how this works. What would you have done? Let me know below."), "offline"


# --------------------------------------------------------------------------
# viral-moment re-ranking
# --------------------------------------------------------------------------
def rank_moments(brain, candidates, log=None):
    """
    Ask the model to rate candidate clips on hook / surprise / ending (0-10 each).
    Returns {index: score_0_to_30} or {} when no AI is reachable.
    """
    if not candidates:
        return {}
    listing = "\n".join(f"[{i}] ({c['end'] - c['start']:.0f}s) {c['text'][:300]}" for i, c in enumerate(candidates))
    prompt = f"""Rate each candidate Short clip transcript for viewer retention.
hook = does the first seconds make you want to keep watching; surprise = unexpected turn or insight;
ending = does it finish on a satisfying point (not cut mid-thought). Scores 0-10.
{listing}
Return JSON: {{"scores": [{{"i": 0, "hook": 0, "surprise": 0, "ending": 0}}, ...]}}"""
    try:
        d, _ = brain.complete_json(prompt, SYSTEM, 0.2, log)
        return {int(r["i"]): float(r["hook"]) + float(r["surprise"]) + float(r["ending"]) for r in d["scores"]}
    except (AIUnavailable, ValueError, KeyError, TypeError):
        return {}


# --------------------------------------------------------------------------
# trend summary
# --------------------------------------------------------------------------
def trend_summary(brain, items, niche="", log=None):
    """items = [{'source','title','detail'}] -> (summary_text, source)."""
    lines = "\n".join(f"- [{i['source']}] {i['title']} {i.get('detail', '')}" for i in items[:40])
    prompt = f"""Creator niche: {niche or 'general'}
Public trend signals:
{lines}
Return JSON: {{"summary": "3-4 sentences on what is trending", "video_ideas": ["5 specific Short ideas the creator could make from their OWN footage or commentary"]}}"""
    try:
        d, src = brain.complete_json(prompt, SYSTEM, 0.8, log)
        ideas = "\n".join(f"• {x}" for x in d.get("video_ideas", []))
        return f"{d.get('summary', '').strip()}\n\nIdeas:\n{ideas}", src
    except (AIUnavailable, ValueError, KeyError, TypeError):
        top = "\n".join(f"• {i['title']}  ({i['source']})" for i in items[:12])
        return f"AI summary unavailable - raw signals:\n{top}", "offline"
