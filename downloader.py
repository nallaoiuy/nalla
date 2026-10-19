"""downloader.py - lightweight 720p downloader built on yt-dlp."""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")


def download_video(url, max_height=720, progress_cb=None, log_cb=None):
    """Download a video (<= max_height) and return the final file path."""
    from yt_dlp import YoutubeDL  # imported here so the app can start before yt-dlp is installed
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    def hook(d):
        if d["status"] == "downloading" and progress_cb:
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                progress_cb(d.get("downloaded_bytes", 0) / total)
        elif d["status"] == "finished" and log_cb:
            log_cb("Download finished, finalizing file...")

    opts = {
        "format": (
            f"bv*[height<={max_height}][ext=mp4]+ba[ext=m4a]/"
            f"b[height<={max_height}][ext=mp4]/b[height<={max_height}]/b"
        ),
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(DOWNLOAD_DIR, "%(title).80B [%(id)s].%(ext)s"),
        "restrictfilenames": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "concurrent_fragment_downloads": 2,  # gentle on weak PCs
        "progress_hooks": [hook],
    }

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
        base, _ = os.path.splitext(path)
        if not os.path.exists(path) and os.path.exists(base + ".mp4"):
            path = base + ".mp4"
        return path
