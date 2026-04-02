from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from threading import Thread
from typing import Any, AsyncGenerator

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl


if sys.platform == "win32":
    # Required for asyncio subprocess support on some Windows/Python setups (e.g. Python 3.13).
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


BASE_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
STATIC_DIR = BASE_DIR / "static"

STANDARD_RESOLUTIONS = [144, 240, 360, 480, 720, 1080, 1440, 2160]


app = FastAPI(title="YouTube Downloader")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class FetchRequest(BaseModel):
    url: HttpUrl


class DownloadRequest(BaseModel):
    url: HttpUrl
    resolution: int


def _ensure_downloads_dir() -> None:
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)


def _yt_dlp_js_runtime_args() -> list[str]:
    """
    yt-dlp increasingly requires a JS runtime for YouTube extraction.
    Prefer Node.js if available; otherwise use Deno if available.
    """
    node = shutil.which("node")
    deno = shutil.which("deno")
    if node:
        return ["--js-runtimes", f"node:{node}"]
    if deno:
        return ["--js-runtimes", f"deno:{deno}"]
    return []


@app.on_event("startup")
def _on_startup() -> None:
    _ensure_downloads_dir()


def _format_mb(num_bytes: int) -> str:
    return f"{num_bytes / (1024 * 1024):.2f} MB"


def _coalesce_int(*vals: Any) -> int:
    for v in vals:
        if isinstance(v, int) and v > 0:
            return v
        if isinstance(v, float) and v > 0:
            return int(v)
    return 0


async def _run_yt_dlp_json(url: str) -> dict[str, Any]:
    def _run() -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["yt-dlp", *_yt_dlp_js_runtime_args(), "-J", url],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    result = await asyncio.to_thread(_run)
    if result.returncode != 0:
        raw = (result.stderr or b"").decode(errors="ignore").strip()
        raw = raw or (result.stdout or b"").decode(errors="ignore").strip()
        msg = raw
        if "No supported JavaScript runtime could be found" in raw:
            msg = (
                "yt-dlp needs a JavaScript runtime for YouTube extraction on your system. "
                "Install Node.js (recommended) or Deno, then restart the server and try again."
            )
        elif "Video unavailable" in raw:
            msg = "Video unavailable/restricted. Try a different YouTube URL."
        raise HTTPException(
            status_code=400,
            detail=msg or "Unable to fetch video info (restricted/unavailable/invalid URL).",
        )

    try:
        return json.loads((result.stdout or b"{}").decode(errors="ignore"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Unable to parse video metadata.")


@app.post("/fetch-resolutions")
async def fetch_resolutions(payload: FetchRequest) -> dict[str, Any]:
    info = await _run_yt_dlp_json(str(payload.url))

    title = info.get("title") or "Untitled"
    formats = info.get("formats") or []

    best_audio_size = 0
    for f in formats:
        if f.get("vcodec") == "none" and f.get("acodec") != "none":
            best_audio_size = max(
                best_audio_size,
                _coalesce_int(f.get("filesize"), f.get("filesize_approx")),
            )

    # For each standard height, pick the biggest video-only filesize we can see.
    best_video_by_height: dict[int, int] = {}
    for f in formats:
        if f.get("vcodec") == "none":
            continue
        height = f.get("height")
        if not isinstance(height, int):
            continue
        if height not in STANDARD_RESOLUTIONS:
            continue
        v_size = _coalesce_int(f.get("filesize"), f.get("filesize_approx"))
        if v_size <= 0:
            continue
        best_video_by_height[height] = max(best_video_by_height.get(height, 0), v_size)

    resolutions: list[dict[str, Any]] = []
    for h in STANDARD_RESOLUTIONS:
        if h in best_video_by_height:
            est = best_video_by_height[h] + best_audio_size
            resolutions.append({"label": f"{h}p — {_format_mb(est)}", "value": h})
        else:
            # Only include heights that are actually present in metadata (even if size unknown)
            if any((f.get("height") == h and f.get("vcodec") != "none") for f in formats):
                resolutions.append({"label": f"{h}p — Size unknown", "value": h})

    if not resolutions:
        resolutions = [{"label": "Best available quality", "value": 9999}]

    return {"title": title, "resolutions": resolutions}


_PERCENT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)%")


async def _sse_events_for_download(url: str, resolution: int) -> AsyncGenerator[bytes, None]:
    _ensure_downloads_dir()

    file_id = uuid.uuid4().hex
    final_filename = f"{file_id}.mp4"
    outtmpl = str(DOWNLOADS_DIR / f"{file_id}.%(ext)s")

    # yt-dlp will merge using ffmpeg when needed.
    format_selector = "bv*+ba/b" if resolution == 9999 else f"bv*[height<={resolution}]+ba/b"

    async def send(obj: dict[str, Any]) -> bytes:
        return f"data: {json.dumps(obj)}\n\n".encode("utf-8")

    q: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def worker() -> None:
        last_percent = -1
        try:
            js_args = _yt_dlp_js_runtime_args()
            proc = subprocess.Popen(
                [
                    "yt-dlp",
                    *js_args,
                    "-f",
                    format_selector,
                    "--merge-output-format",
                    "mp4",
                    "--newline",
                    "-o",
                    outtmpl,
                    url,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            assert proc.stdout is not None
            for line in proc.stdout:
                m = _PERCENT_RE.search(line)
                if not m:
                    continue
                pct = int(float(m.group(1)))
                pct = max(0, min(100, pct))
                if pct != last_percent:
                    last_percent = pct
                    loop.call_soon_threadsafe(q.put_nowait, {"percent": pct, "status": "downloading"})

            rc = proc.wait()
            if rc != 0:
                loop.call_soon_threadsafe(
                    q.put_nowait,
                    {"percent": max(last_percent, 0), "status": "error", "message": "Download failed."},
                )
                loop.call_soon_threadsafe(q.put_nowait, None)
                return

            produced = DOWNLOADS_DIR / final_filename
            if not produced.exists():
                matches = list(DOWNLOADS_DIR.glob(f"{file_id}.*"))
                if matches:
                    matches[0].rename(produced)

            if not produced.exists():
                loop.call_soon_threadsafe(
                    q.put_nowait,
                    {"percent": 100, "status": "error", "message": "Download completed but file not found."},
                )
                loop.call_soon_threadsafe(q.put_nowait, None)
                return

            loop.call_soon_threadsafe(q.put_nowait, {"percent": 100, "status": "done", "filename": final_filename})
            loop.call_soon_threadsafe(q.put_nowait, None)
        except FileNotFoundError:
            loop.call_soon_threadsafe(
                q.put_nowait,
                {"percent": 0, "status": "error", "message": "yt-dlp not found. Install it and ensure it's on PATH."},
            )
            loop.call_soon_threadsafe(q.put_nowait, None)
        except Exception:
            loop.call_soon_threadsafe(q.put_nowait, {"percent": 0, "status": "error", "message": "Download crashed."})
            loop.call_soon_threadsafe(q.put_nowait, None)

    Thread(target=worker, daemon=True).start()

    while True:
        item = await q.get()
        if item is None:
            break
        yield await send(item)


@app.post("/download")
async def download(payload: DownloadRequest) -> StreamingResponse:
    async def gen() -> AsyncGenerator[bytes, None]:
        async for ev in _sse_events_for_download(str(payload.url), payload.resolution):
            yield ev

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/file/{filename}")
async def get_file(filename: str, background: BackgroundTasks) -> FileResponse:
    _ensure_downloads_dir()

    safe_name = Path(filename).name
    file_path = (DOWNLOADS_DIR / safe_name).resolve()
    if not str(file_path).startswith(str(DOWNLOADS_DIR.resolve())):
        raise HTTPException(status_code=400, detail="Invalid filename.")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found.")

    def _cleanup() -> None:
        try:
            file_path.unlink(missing_ok=True)
        except Exception:
            pass

    background.add_task(_cleanup)
    return FileResponse(path=str(file_path), media_type="video/mp4", filename=safe_name, background=background)

