"""
analytics.py - learn from your own results.

  record_upload()   after every upload, store the video id + HOW it was made
                    (captions, music, SFX, zoom, clip length, headline type...)
  refresh_stats()   pull views / watch-time numbers through the YouTube Data +
                    Analytics APIs (needs the extra read-only permissions; the app
                    will ask you to sign in again once)
  insights()        compare groups of your own videos and print plain-language
                    patterns, which are also fed to the AI title/description prompts.

Caution: with only a handful of videos these are hints, not proof. Differences
may come from topic, thumbnail, timing or luck rather than the editing choice.
"""
import datetime
from statistics import mean

import config

LOG = "uploads.json"


def load_uploads():
    return config.read_data_json(LOG, [])


def record_upload(video_id, url, title, privacy, publish_at, meta, source=""):
    data = load_uploads()
    data.append({"video_id": video_id, "url": url, "title": title, "privacy": privacy,
                 "publish_at": publish_at.isoformat() if publish_at else None,
                 "uploaded_at": datetime.datetime.utcnow().isoformat(timespec="seconds"),
                 "source": source, "meta": meta or {}, "stats": {}})
    config.write_data_json(LOG, data)


def refresh_stats(log=None):
    """Fetch statistics for uploaded videos. Returns the number of videos updated."""
    log = log or (lambda m: None)
    import uploader   # lazy: needs the Google libraries
    ups = [u for u in load_uploads() if u.get("video_id")]
    if not ups:
        return 0
    yt, ana = uploader.get_services()
    ids = [u["video_id"] for u in ups][-50:]
    resp = yt.videos().list(part="statistics", id=",".join(ids)).execute()
    basic = {v["id"]: v.get("statistics", {}) for v in resp.get("items", [])}
    deep = {}
    try:
        start = min(u["uploaded_at"][:10] for u in ups if u["video_id"] in ids)
        r = ana.reports().query(
            ids="channel==MINE", startDate=start, endDate=datetime.date.today().isoformat(),
            metrics="views,averageViewDuration,averageViewPercentage,likes,comments,subscribersGained",
            dimensions="video", filters="video==" + ",".join(ids), maxResults=50).execute()
        keys = ["views", "avg_duration", "avg_pct", "likes", "comments", "subs"]
        for row in r.get("rows", []):
            deep[row[0]] = dict(zip(keys, row[1:]))
    except Exception as e:
        log(f"Analytics API unavailable ({str(e)[:80]}); using basic statistics only.")
    n = 0
    now = datetime.datetime.utcnow().isoformat(timespec="seconds")
    for u in ups:
        st = {}
        b = basic.get(u["video_id"])
        if b:
            st.update(views=int(b.get("viewCount", 0)), likes=int(b.get("likeCount", 0)),
                      comments=int(b.get("commentCount", 0)))
        st.update(deep.get(u["video_id"], {}))
        if st:
            u["stats"], u["stats_at"] = st, now
            n += 1
    all_ups = load_uploads()
    by_id = {u["video_id"]: u for u in ups}
    for u in all_ups:
        if u.get("video_id") in by_id:
            u.update(stats=by_id[u["video_id"]]["stats"], stats_at=by_id[u["video_id"]].get("stats_at"))
    config.write_data_json(LOG, all_ups)
    return n


# --------------------------------------------------------------------------
# Insights (pure functions - easy to test)
# --------------------------------------------------------------------------
def _features(u):
    m = u.get("meta") or {}
    length = m.get("clip_len", 0) or 0
    bucket = "under 20 s" if length < 20 else "20-40 s" if length < 40 else "40-60 s" if length < 60 else "over 60 s"
    return {
        "caption style": m.get("captions") or "Off",
        "background music": "with music" if m.get("bgm") else "no music",
        "sound effects": "with SFX" if m.get("sfx") else "no SFX",
        "zoom": m.get("zoom") or "Off",
        "commentary / voice-over": "with commentary" if m.get("voiceover") else "no commentary",
        "headline": "question headline" if m.get("question_hook") else "statement / none",
        "clip length": bucket,
        "card style": m.get("card_style") or "?",
    }


def insights(uploads, min_videos=4, min_group=2):
    """-> {'ready','n','metric','lines':[text],'hints':'one-line summary for AI prompts'}"""
    rated = [(u, _features(u)) for u in uploads if u.get("stats")]
    if len(rated) < min_videos:
        return {"ready": False, "n": len(rated), "metric": "", "hints": "",
                "lines": [f"Need at least {min_videos} uploaded Shorts with statistics (you have {len(rated)}). "
                          "Upload, wait a few days, then press Refresh."]}
    use_pct = all("avg_pct" in u["stats"] for u, _ in rated)
    key, label = ("avg_pct", "average % viewed") if use_pct else ("views", "views")
    fmt = (lambda v: f"{v:.0f}%") if use_pct else (lambda v: f"{v:,.0f}")
    lines, hints = [], []
    for feat in rated[0][1]:
        groups = {}
        for u, f in rated:
            groups.setdefault(f[feat], []).append(u["stats"].get(key, 0))
        ok = {k: v for k, v in groups.items() if len(v) >= min_group}
        if len(ok) < 2:
            continue
        best = max(ok, key=lambda k: mean(ok[k]))
        worst = min(ok, key=lambda k: mean(ok[k]))
        lo = mean(ok[worst])
        if lo > 0 and (mean(ok[best]) - lo) / lo >= 0.15:
            lines.append(f"{feat}: \"{best}\" averaged {fmt(mean(ok[best]))} {label} vs {fmt(lo)} for "
                         f"\"{worst}\" ({len(ok[best])} vs {len(ok[worst])} videos)")
            hints.append(f"{feat} \"{best}\" performed better")
    return {"ready": True, "n": len(rated), "metric": label,
            "lines": lines or ["No clear pattern yet - keep publishing and check again."],
            "hints": "; ".join(hints)[:300]}
