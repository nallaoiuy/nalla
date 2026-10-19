"""
diagnostics.py - "does the pipeline actually work on THIS PC?" self-test.

Checks FFmpeg, the filters/encoders the app relies on, Python packages, free
disk space and fonts, then renders a 3-second synthetic clip through the whole
pipeline (cards, captions, SFX, zoom, BGM-less mix) and reports how long it took.
Results are [(level, message)] with level 'ok' | 'warn' | 'error'.
"""
import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import time

import captions
import config
import overlays
import processor

_NOWIN = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _ff(args):
    return subprocess.run([processor.find_ffmpeg(), "-hide_banner", *args], capture_output=True,
                          text=True, creationflags=_NOWIN)


def run_diagnostics(log=None):
    res = []
    add = lambda lvl, msg: (res.append((lvl, msg)), log and log(f"[{lvl}] {msg}"))

    # ---- FFmpeg
    try:
        ver = _ff(["-version"]).stdout.splitlines()[0]
        add("ok", ver[:80])
    except Exception as e:
        add("error", f"FFmpeg not found: {e}")
        return res
    filters = _ff(["-filters"]).stdout
    for f, why in (("ass", "animated captions"), ("sidechaincompress", "audio ducking"),
                   ("amix", "audio mixing"), ("boxblur", "blurred background"), ("overlay", "cards")):
        add("ok" if f" {f} " in filters else "error", f"FFmpeg filter '{f}' ({why})")
    encs = _ff(["-encoders"]).stdout
    add("ok" if "libx264" in encs else "error", "Encoder libx264 (CPU)")
    try:
        qsv = processor.detect_encoder("Auto") == "h264_qsv"
    except Exception:
        qsv = False
    add("ok" if qsv else "warn", "Intel Quick Sync (h264_qsv) works" if qsv else
        "h264_qsv not available - using libx264 (slower but fine)")

    # ---- packages
    for mod, need in (("customtkinter", True), ("PIL", True), ("yt_dlp", True),
                      ("googleapiclient", False), ("google_auth_oauthlib", False),
                      ("faster_whisper", False)):
        try:
            importlib.import_module(mod)
            add("ok", f"Python package {mod}")
        except ImportError:
            add("error" if need else "warn",
                f"Python package {mod} missing" + ("" if need else " (optional - needed for "
                f"{'uploads/analytics' if 'google' in mod else 'local transcription'})"))

    # ---- fonts / disk
    font = next((f for f in overlays.FONT_CANDIDATES if os.path.exists(f)), None)
    add("ok" if font else "warn", f"Card font: {os.path.basename(font)}" if font else
        "No bold system font found - cards will use a tiny default font")
    for label, path in (("project drive", processor.BASE_DIR), ("settings drive", config.DATA_DIR)):
        free = shutil.disk_usage(path).free / 1e9
        add("ok" if free > 3 else "warn", f"{free:.1f} GB free on {label}")
    add("ok", f"API keys stored with: {config.secret_backend()}" +
        (" (not encrypted on this OS)" if config.secret_backend() == "plain" else ""))

    # ---- end-to-end render
    tmp = tempfile.mkdtemp(prefix="vas_diag_")
    try:
        src = os.path.join(tmp, "diag.mp4")
        r = subprocess.run([processor.find_ffmpeg(), "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                            "testsrc2=s=1280x720:d=3:r=25", "-f", "lavfi", "-i", "sine=f=440:d=3",
                            "-c:v", "libx264", "-c:a", "aac", "-shortest", src],
                           capture_output=True, creationflags=_NOWIN)
        if r.returncode != 0:
            add("error", "Could not create the test clip.")
            return res
        words = captions.words_from_segments([{"start": 0.2, "end": 2.8, "text": "This is a quick self test of captions"}])
        t = time.time()
        meta = {}
        out = processor.make_short(src, "0:00", "0:03", headline="Self test", stat="All systems go",
                                   caption_style="Pop Highlight", sfx_on=True, zoom="Slow push",
                                   encoder="Auto", words=words, meta=meta)
        secs = time.time() - t
        dur = processor.get_duration(out) or 0
        ok = abs(dur - 3) < 0.3
        add("ok" if ok else "error", f"Test render of a 3 s clip took {secs:.1f} s (output {dur:.1f} s)")
        if secs > 30:
            add("warn", "Rendering is slow here - use shorter clips, 480p downloads, and fewer effects.")
        os.remove(out)
    except Exception as e:
        add("error", f"Test render failed: {str(e)[-300:]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return res
