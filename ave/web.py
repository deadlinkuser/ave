# [NOTE] FastAPI Web GUI server for AVE (port 8488) with optional LAN Basic Auth, live telemetry, and presets.

from __future__ import annotations

import base64
import os
import secrets
import shutil
import socket
import urllib.parse
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from .models import Preset
from .worker import BatchWorker

BASE_DIR = Path(os.environ.get("AVE_DATA_DIR", Path.home() / "containers" / "ave"))
TEMPLATES_DIR = Path(__file__).parent / "templates"
WEB_PASSWORD = os.environ.get("AVE_WEB_PASSWORD", "").strip()
WEB_USER = os.environ.get("AVE_WEB_USER", "ave").strip() or "ave"

LANGUAGE_CHOICES = [
    ("all", "All Existing Tracks"),
    ("eng", "English"),
    ("jpn", "Japanese"),
    ("spa", "Spanish"),
    ("fre", "French"),
    ("ger", "German"),
    ("ita", "Italian"),
    ("kor", "Korean"),
    ("chi", "Chinese"),
    ("por", "Portuguese"),
    ("rus", "Russian"),
]

app = FastAPI(title="AVE — Automatic Video Encoder", version="1.0.0")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
worker = BatchWorker(BASE_DIR)


@app.middleware("http")
async def optional_lan_auth(request: Request, call_next):
    # [NOTE] Enforces HTTP Basic Auth per browser session only when AVE_WEB_PASSWORD is configured.
    if not WEB_PASSWORD:
        return await call_next(request)

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header.split(" ", 1)[1]).decode("utf-8")
            user, passwd = decoded.split(":", 1)
            if secrets.compare_digest(user, WEB_USER) and secrets.compare_digest(passwd, WEB_PASSWORD):
                return await call_next(request)
        except Exception:
            pass

    return Response(
        content="Authentication required to access AVE.",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="AVE — Automatic Video Encoder"'},
    )


def format_hhmmss(total_seconds: int) -> str:
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def get_system_telemetry() -> dict[str, str]:
    load_1m, _, _ = os.getloadavg()
    cores = os.cpu_count() or 1
    cpu_pct = min(100, int((load_1m / cores) * 100))

    mem_str = "N/A"
    try:
        meminfo = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, val = line.split(":", 1)
            meminfo[key.strip()] = int(val.strip().split()[0])
        total_kb = meminfo.get("MemTotal", 1)
        avail_kb = meminfo.get("MemAvailable", total_kb)
        used_gb = (total_kb - avail_kb) / (1024 * 1024)
        total_gb = total_kb / (1024 * 1024)
        mem_str = f"{used_gb:.1f} / {total_gb:.1f} GB"
    except Exception:
        pass

    disk = shutil.disk_usage(worker.output_dir)
    free_gb = disk.free / (1024**3)
    return {
        "cpu": f"{cpu_pct}%",
        "ram": mem_str,
        "disk_free": f"{free_gb:.1f} GB free",
    }

def _dashboard_context(request: Request) -> dict[str, object]:
    cfg = worker.config_mgr.load()
    presets = worker.preset_mgr.load_all()
    pending_files = [
        p.relative_to(worker.watch_dir).as_posix()
        for p in worker.scanner.list_candidates(priority_top_folder=worker.active_top_folder)
    ]
    failed_reports = []
    failed_dir = worker.watch_dir / "_failed"
    if failed_dir.exists():
        for err_file in sorted(failed_dir.rglob("*.error.txt")):
            content = err_file.read_text(encoding="utf-8", errors="replace")
            issue_title = urllib.parse.quote(f"Encode failure: {err_file.stem}")
            issue_body = urllib.parse.quote(f"```\n{content[:1200]}\n```")
            github_url = f"https://github.com{issue_title}&body={issue_body}"
            failed_reports.append({
                "name": err_file.name.replace(".error.txt", ""),
                "details": content,
                "github_url": github_url,
            })

    reclaimable_gb = worker.get_reclaimable_originals_bytes() / (1024**3)
    return {
        "request": request,
        "cfg": cfg,
        "status": worker.runner.status,
        "batch_timer": format_hhmmss(worker.runner.status.batch_encode_sec),
        "file_eta": format_hhmmss(worker.runner.status.file_eta_sec),
        "telemetry": get_system_telemetry(),
        "presets": presets,
        "language_choices": LANGUAGE_CHOICES,
        "pending_files": pending_files,
        "failed_reports": failed_reports,
        "reclaimable_gb": f"{reclaimable_gb:.2f}",
    }


@app.on_event("startup")
def on_startup() -> None:
    worker.start_background_loop()


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", _dashboard_context(request))


@app.get("/partials/live", response_class=HTMLResponse)
def live_partial(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "partials/live.html", _dashboard_context(request))


@app.post("/api/start")
def api_start(preset_id: str | None = Form(default=None)) -> RedirectResponse:
    worker.trigger_start(preset_id=preset_id)
    return RedirectResponse(url="/", status_code=303)


@app.post("/api/stop")
def api_stop(mode: str = Form(default="graceful")) -> RedirectResponse:
    worker.request_stop(hard=(mode == "hard"))
    return RedirectResponse(url="/", status_code=303)


@app.post("/api/presets/default")
def api_set_default_preset(preset_id: str = Form(...)) -> RedirectResponse:
    worker.config_mgr.set_default_preset(preset_id)
    worker.runner.status.active_preset_id = preset_id
    worker.runner._save_status()
    return RedirectResponse(url="/", status_code=303)


@app.post("/api/presets/create")
def api_create_preset(
    name: str = Form(...),
    container: str = Form("mkv"),
    max_height: str = Form("1080"),
    video_codec: str = Form("hevc"),
    bit_depth: int = Form(10),
    crf: int = Form(22),
    speed_preset: str = Form("fast"),
    audio_codec: str = Form("aac"),
    audio_languages: list[str] = Form(default=["eng"]),
    subtitle_languages: list[str] = Form(default=["eng"]),
    passthrough_max_mb: str = Form(""),
    error_cleanup_remux: bool = Form(False),
    make_default: bool = Form(False),
) -> RedirectResponse:
    height_val = None if max_height == "original" else int(max_height)
    pt_val = int(passthrough_max_mb) if passthrough_max_mb.strip().isdigit() else None

    final_audio = ["all"] if "all" in audio_languages else (audio_languages or ["eng"])
    final_subs = ["all"] if "all" in subtitle_languages else (subtitle_languages or ["eng"])

    new_preset = Preset(
        id="",
        name=name.strip(),
        container=container,  # type: ignore[arg-type]
        max_height=height_val,
        video_codec=video_codec,  # type: ignore[arg-type]
        bit_depth=10 if bit_depth == 10 else 8,
        crf=crf,
        speed_preset=speed_preset if speed_preset in {"slow", "medium", "fast"} else "fast",
        audio_codec=audio_codec,  # type: ignore[arg-type]
        audio_languages=final_audio,
        subtitle_languages=final_subs,
        passthrough_max_mb=pt_val,
        error_cleanup_remux=error_cleanup_remux,
    )
    saved = worker.preset_mgr.save_custom_preset(new_preset)
    if make_default:
        worker.config_mgr.set_default_preset(saved.id)
    return RedirectResponse(url="/", status_code=303)


@app.post("/api/presets/delete")
def api_delete_preset(preset_id: str = Form(...)) -> RedirectResponse:
    worker.preset_mgr.delete_custom_preset(preset_id)
    cfg = worker.config_mgr.load()
    if cfg.default_preset_id == preset_id:
        worker.config_mgr.set_default_preset("builtin_hevc_balanced")
    return RedirectResponse(url="/", status_code=303)


@app.get("/api/presets/export")
def api_export_presets() -> Response:
    raw = worker.preset_mgr.export_json()
    return Response(
        content=raw,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="ave-custom-presets.json"'},
    )


@app.post("/api/presets/import")
async def api_import_presets(file: UploadFile = File(...)) -> RedirectResponse:
    raw_bytes = await file.read()
    try:
        count = worker.preset_mgr.import_json(raw_bytes.decode("utf-8"), merge=True)
        worker.runner.status.banner_message = f"Imported {count} custom preset(s) successfully."
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid preset file: {exc}") from exc
    return RedirectResponse(url="/", status_code=303)


@app.post("/api/originals/clear")
def api_clear_originals() -> RedirectResponse:
    if worker.runner.status.state.value not in {"idle", "stopped"}:
        worker.runner.status.banner_message = "Cannot clear originals while a batch is running."
        return RedirectResponse(url="/", status_code=303)
    count, reclaimed = worker.clear_originals()
    gb = reclaimed / (1024**3)
    worker.runner.status.banner_message = f"Cleared {count} original file(s) ({gb:.2f} GB reclaimed)."
    return RedirectResponse(url="/", status_code=303)


@app.post("/api/settings")
def api_update_settings(
    enable_ram_scratch: bool = Form(False),
    thermal_cooldown_sec: int = Form(0),
    write_batch_summaries: bool = Form(False),
    theme: str = Form("dark"),
) -> RedirectResponse:
    cfg = worker.config_mgr.load()
    cfg.enable_ram_scratch = enable_ram_scratch
    cfg.thermal_cooldown_sec = max(0, min(30, thermal_cooldown_sec))
    cfg.write_batch_summaries = write_batch_summaries
    cfg.theme = "light" if theme == "light" else "dark"
    worker.config_mgr.save(cfg)
    return RedirectResponse(url="/", status_code=303)


@app.post("/api/banner/dismiss", response_class=PlainTextResponse)
def api_dismiss_banner() -> str:
    worker.runner.status.banner_message = None
    worker.runner._save_status()
    return ""


def find_available_port(preferred: int = 8488) -> int:
    port = preferred
    while port < preferred + 50:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
        port += 1
    return preferred
