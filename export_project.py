"""
export_project.py - make a zip of the project that is safe to share.

Run:  python export_project.py
Leaves out downloads, output, tmp, ffmpeg binaries, settings and anything that
looks like a secret. (Your real keys/tokens already live outside this folder.)
"""
import os
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
SKIP_DIRS = {"downloads", "output", "tmp", "__pycache__", ".git", "ffmpeg"}
SKIP_FILES = {"ffmpeg.exe", "ffprobe.exe", "settings.json", "token.json", "client_secrets.json",
              "secrets.json", "uploads.json", "usage.json"}
SKIP_EXT = (".pyc", ".mp4", ".mkv", ".webm", ".mov", ".zip")


def main():
    out = os.path.join(BASE, "LocalVideoStudio_share.zip")
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(BASE):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                if f in SKIP_FILES or f.lower().endswith(SKIP_EXT) or "secret" in f.lower() or "token" in f.lower():
                    continue
                z.write(os.path.join(root, f), os.path.relpath(os.path.join(root, f), BASE))
                n += 1
    print(f"Wrote {out} ({n} files). Secrets, media and FFmpeg were excluded.")
    return out


if __name__ == "__main__":
    main()
