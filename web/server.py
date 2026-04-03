"""FastAPI backend for the native (dockerless) op-replay-clipper."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import socket
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests as http_requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel

app = FastAPI(title="OP Replay Clipper")
log = logging.getLogger("clipper.server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

# ---------------------------------------------------------------------------
# Native configuration (replaces Docker env vars)
# ---------------------------------------------------------------------------

# Base directory for all clipper data.
CLIPPER_HOME = Path(os.environ.get("CLIPPER_HOME", Path.home() / ".op-replay-clipper"))

# Where job output files are written.  Each job gets a subdirectory.
OUTPUT_DIR = Path(os.environ.get("CLIPPER_OUTPUT_DIR", CLIPPER_HOME / "output"))

# Where downloaded route data lives.
DATA_DIR = Path(os.environ.get("CLIPPER_DATA_DIR", CLIPPER_HOME / "data"))

# openpilot checkout used by clip.py.
OPENPILOT_DIR = Path(os.environ.get("OPENPILOT_ROOT", CLIPPER_HOME / "openpilot"))

# Path to the clipper project root (where clip.py lives).
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Detect GPU by checking nvidia-smi at startup.
_has_gpu: bool | None = None


def _detect_gpu() -> bool:
    global _has_gpu
    if _has_gpu is None:
        _has_gpu = shutil.which("nvidia-smi") is not None and os.system("nvidia-smi >/dev/null 2>&1") == 0
    return _has_gpu


VALID_RENDER_TYPES = {
    "ui", "ui-alt", "driver-debug", "forward", "wide",
    "driver", "360", "forward_upon_wide", "360_forward_upon_wide",
}

# Regex to strip ANSI escape sequences (color codes, cursor movement, etc.)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\].*?\x07")


def _sanitize_log_line(raw: str) -> str | None:
    """Clean a raw subprocess output line for display in the web UI.

    Strips ANSI escapes, carriage returns (tqdm progress overwrites),
    and other control characters. Returns None for lines that are
    empty after sanitization (suppresses noise in the log view).
    """
    # Handle carriage-return overwrites: keep only the last segment
    if "\r" in raw:
        raw = raw.rsplit("\r", 1)[-1]
    # Strip ANSI escape sequences
    raw = _ANSI_RE.sub("", raw)
    # Strip remaining control characters (except tab)
    raw = "".join(c for c in raw if c == "\t" or (c >= " " and c != "\x7f"))
    raw = raw.rstrip()
    return raw if raw else None
SMEAR_RENDER_TYPES = {"ui", "ui-alt", "driver-debug"}


# ---------------------------------------------------------------------------
# Job tracking (unchanged from original)
# ---------------------------------------------------------------------------

class JobState(str, Enum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"


_FFMPEG_PROGRESS_RE = re.compile(
    r"(?:frame=\s*(\d+))?\s*"
    r"(?:fps=\s*([\d.]+))?\s*"
    r"(?:.*?size=\s*(\d+)kB)?\s*"
    r"(?:.*?time=\s*([\d:.]+))?\s*"
    r"(?:.*?bitrate=\s*([\d.]+)kbits/s)?\s*"
    r"(?:.*?speed=\s*([\d.]+)x)?"
)


def _parse_ffmpeg_time(t: str) -> float:
    """Convert HH:MM:SS.ss to seconds."""
    parts = t.split(":")
    if len(parts) == 3:
        return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
    return 0.0


@dataclass
class Job:
    job_id: str
    state: JobState = JobState.queued
    logs: list[str] = field(default_factory=list)
    output_path: str = ""
    error: str = ""
    progress: dict[str, Any] = field(default_factory=dict)


JOBS: dict[str, Job] = {}


# ---------------------------------------------------------------------------
# Request / Response models (unchanged from original)
# ---------------------------------------------------------------------------

class ClipRequestBody(BaseModel):
    route: str
    render_type: str = "ui"
    file_size_mb: int = 9
    file_format: str = "auto"
    smear_seconds: int = 3
    jwt_token: str = ""
    download_source: str = "connect"
    device_ip: str = ""
    ssh_port: int = 22


class JobResponse(BaseModel):
    job_id: str
    state: str


# ---------------------------------------------------------------------------
# Native clip.py invocation (replaces _build_docker_cmd + _run_container)
# ---------------------------------------------------------------------------

def _build_clip_cmd(job: Job, req: ClipRequestBody) -> list[str]:
    """Build the ``uv run python clip.py`` command for native execution.

    This replaces ``_build_docker_cmd()`` from the Docker version.
    Instead of ``docker run ... <image> <args>``, we invoke clip.py directly
    as a subprocess using ``uv run`` within the clipper's project directory.
    """
    job_dir = OUTPUT_DIR / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    output_path = str(job_dir / "output.mp4")

    cmd: list[str] = [
        "uv", "run", "python", "clip.py",
        req.render_type,
        req.route,
        "-o", output_path,
        "-m", str(req.file_size_mb),
        "--file-format", req.file_format,
        "--openpilot-dir", str(OPENPILOT_DIR),
        "--skip-openpilot-update",
        "--skip-openpilot-bootstrap",
        "--data-root", str(DATA_DIR),
    ]

    # GPU acceleration
    if _detect_gpu():
        cmd.extend(["--accel", "nvidia"])
    else:
        cmd.extend(["--accel", "cpu"])

    # Smear/preroll for UI render types
    if req.render_type in SMEAR_RENDER_TYPES:
        cmd.extend(["--smear-seconds", str(req.smear_seconds)])

    # JWT token for private routes (only for connect downloads)
    if req.jwt_token and req.download_source != "ssh":
        cmd.extend(["-j", req.jwt_token])

    # SSH download from comma device on LAN
    if req.download_source == "ssh":
        cmd.extend([
            "--download-source", "ssh",
            "--device-ip", req.device_ip,
            "--ssh-port", str(req.ssh_port),
        ])

    return cmd


async def _run_clip(job: Job, req: ClipRequestBody) -> None:
    """Run clip.py as a native subprocess and stream output into the job log.

    This replaces ``_run_container()`` from the Docker version.  The async
    stdout streaming and ffmpeg progress parsing logic is preserved — only
    the command source changes (subprocess instead of Docker container).
    """
    try:
        cmd = _build_clip_cmd(job, req)
        job.state = JobState.running
        job.logs.append(f"$ uv run python clip.py {req.render_type} {req.route} ...")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(PROJECT_ROOT),
        )

        assert proc.stdout is not None
        async for raw_line in proc.stdout:
            decoded = raw_line.decode("utf-8", errors="replace").rstrip("\n")

            # Parse ffmpeg progress lines before sanitization (they use \r)
            # Match lines starting with frame= or size= (trim pass uses size= without frame=)
            frame_content = decoded
            if "\r" in frame_content:
                frame_content = frame_content.rsplit("\r", 1)[-1]
            stripped = frame_content.lstrip()
            if stripped.startswith("frame=") or (stripped.startswith("size=") and "kB" in stripped):
                m = _FFMPEG_PROGRESS_RE.match(stripped)
                progress: dict[str, Any] = {}
                if m:
                    if m.group(3):
                        progress["size_kb"] = int(m.group(3))
                    if m.group(4):
                        progress["time_seconds"] = round(_parse_ffmpeg_time(m.group(4)), 1)
                    if m.group(5):
                        progress["bitrate_kbps"] = float(m.group(5))
                    if m.group(6):
                        progress["speed"] = float(m.group(6))
                # Fallback: parse size=/time=/bitrate= from trim pass lines
                if not progress and "size=" in stripped:
                    sm = re.search(r"size=\s*(\d+)kB", stripped)
                    tm = re.search(r"time=\s*([\d:.]+)", stripped)
                    bm = re.search(r"bitrate=\s*([\d.]+)kbits/s", stripped)
                    if sm:
                        progress["size_kb"] = int(sm.group(1))
                    if tm:
                        progress["time_seconds"] = round(_parse_ffmpeg_time(tm.group(1)), 1)
                    if bm:
                        progress["bitrate_kbps"] = float(bm.group(1))
                if progress:
                    job.progress = progress

            # Sanitize for display (strip ANSI, \r, control chars)
            line = _sanitize_log_line(decoded)
            if line is not None:
                job.logs.append(line)

        exit_code = await proc.wait()

        output_path = OUTPUT_DIR / job.job_id / "output.mp4"
        file_found = output_path.exists()
        file_size = output_path.stat().st_size if file_found else 0
        if exit_code == 0 and file_found and file_size > 0:
            job.state = JobState.done
            job.output_path = str(output_path)
            job.logs.append("Render complete.")
        else:
            job.state = JobState.failed
            job.error = f"clip.py exited with code {exit_code}"
            job.logs.append(f"ERROR: {job.error}")
            job.logs.append(f"DEBUG: output_path={output_path}, exists={file_found}, size={file_size}")
            job_dir = OUTPUT_DIR / job.job_id
            if job_dir.exists():
                contents = list(job_dir.iterdir())
                job.logs.append(f"DEBUG: job_dir contents={[f.name for f in contents]}")
    except FileNotFoundError:
        job.state = JobState.failed
        job.error = "uv not found. Is uv installed?"
        job.logs.append(f"ERROR: {job.error}")
    except Exception as exc:
        job.state = JobState.failed
        job.error = str(exc)
        job.logs.append(f"ERROR: {job.error}")


# ---------------------------------------------------------------------------
# Device discovery helpers (unchanged from original)
# ---------------------------------------------------------------------------

_SCAN_PORTS = [8022, 22]  # comma 3X on 8022, comma 4 on 22
_SCAN_TIMEOUT = 0.5
_DEVICE_TYPE = {8022: "comma 3X", 22: "comma 4"}


def _detect_subnet() -> str | None:
    """Detect the LAN subnet to scan."""
    # Check explicit env var first
    host_lan_ip = os.environ.get("HOST_LAN_IP", "")
    if host_lan_ip and not host_lan_ip.startswith("127."):
        log.info("HOST_LAN_IP=%s", host_lan_ip)
        return host_lan_ip.rsplit(".", 1)[0] + "."

    # Native: detect via UDP route (no Docker host.docker.internal needed)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 53))
            ip = s.getsockname()[0]
            if not ip.startswith("127."):
                log.info("UDP route=%s", ip)
                return ip.rsplit(".", 1)[0] + "."
    except OSError:
        pass

    return None


def _check_port(ip: str, port: int) -> dict[str, Any] | None:
    """Try to connect to ip:port and verify it's an SSH service."""
    try:
        with socket.create_connection((ip, port), timeout=_SCAN_TIMEOUT) as s:
            s.settimeout(_SCAN_TIMEOUT)
            banner = s.recv(256).decode("utf-8", errors="replace").strip()
            if "SSH" in banner.upper():
                return {"ip": ip, "port": port, "device_type": _DEVICE_TYPE.get(port, "unknown"), "banner": banner}
    except (OSError, socket.timeout):
        pass
    return None


def _scan_for_devices() -> dict[str, Any]:
    """Scan LAN for comma devices."""
    start_time = time.time()

    subnet = _detect_subnet()
    if not subnet:
        log.warning("Could not detect LAN subnet")
        return {"devices": [], "error": "Could not detect local network subnet."}

    log.info("Scanning subnet %s0/24", subnet)

    ip_order = list(range(2, 51)) + list(range(51, 255)) + [1]
    targets = [(f"{subnet}{i}", port) for i in ip_order for port in _SCAN_PORTS]

    devices: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=32) as pool:
        futures = {pool.submit(_check_port, ip, port): (ip, port) for ip, port in targets}
        for future in futures:
            result = future.result()
            if result is not None:
                devices.append(result)
                log.info("FOUND %s at %s:%d", result["device_type"], result["ip"], result["port"])
                for f in futures:
                    f.cancel()
                break

    elapsed = round(time.time() - start_time, 1)
    log.info("Scan complete: %d device(s) in %.1fs", len(devices), elapsed)
    return {"devices": devices, "elapsed": elapsed}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html_path = Path(__file__).parent / "static" / "index.html"
    return HTMLResponse(content=html_path.read_text())


@app.get("/api/health")
async def health() -> dict[str, Any]:
    """Health check — reports native environment status instead of Docker."""
    gpu = _detect_gpu()
    uv_ok = shutil.which("uv") is not None
    openpilot_ok = (OPENPILOT_DIR / ".venv" / "bin" / "python").exists()
    clip_ok = (PROJECT_ROOT / "clip.py").exists()
    return {
        "native": True,
        "uv": uv_ok,
        "openpilot": openpilot_ok,
        "clip_py": clip_ok,
        "gpu": gpu,
        "openpilot_dir": str(OPENPILOT_DIR),
        "output_dir": str(OUTPUT_DIR),
    }


@app.post("/api/scan-devices")
async def scan_devices() -> dict[str, Any]:
    """Scan the local network for comma devices with SSH enabled."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _scan_for_devices)


class TestSSHRequest(BaseModel):
    ip: str
    port: int = 22


@app.post("/api/test-ssh")
async def test_ssh(body: TestSSHRequest) -> dict[str, Any]:
    """Test SSH connectivity to a specific device."""
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _check_port, body.ip, body.port)
    if result:
        return {"success": True, "message": f"Connected — {result['device_type']} detected", "device_type": result["device_type"]}
    return {"success": False, "message": f"Cannot reach device at {body.ip}:{body.port}"}


class EstimateRequest(BaseModel):
    route: str
    file_size_mb: int = 9
    render_type: str = "ui"
    jwt_token: str = ""
    download_source: str = "connect"


# Approximate bitrate in Kbps at QP/CRF 10 per render type.
_MAX_QUALITY_BITRATE_KBPS: dict[str, int] = {
    "ui": 40_000,
    "ui-alt": 40_000,
    "driver-debug": 30_000,
    "forward": 20_000,
    "wide": 20_000,
    "driver": 15_000,
    "360": 35_000,
    "forward_upon_wide": 22_000,
    "360_forward_upon_wide": 60_000,
}


def _resolve_route_duration(route_url: str, jwt_token: str = "") -> int | None:
    """Resolve the duration in seconds from a route URL."""
    route_url = route_url.strip()

    if "|" in route_url and not route_url.startswith("http"):
        return None

    if not route_url.startswith("https://connect.comma.ai/"):
        return None

    parsed = urlparse(route_url)
    parts = parsed.path.split("/")

    # /dongle/start_ms/end_ms (absolute time)
    if len(parts) == 4 and "-" not in parts[2]:
        try:
            start_ms = int(parts[2])
            end_ms = int(parts[3])
            return max(1, (end_ms - start_ms) // 1000)
        except ValueError:
            return None

    # /dongle/route-name/start/end (relative time)
    if len(parts) == 5 and "-" in parts[2]:
        try:
            return max(1, int(parts[4]) - int(parts[3]))
        except ValueError:
            return None

    # /dongle/route-name (full route — needs API lookup)
    if len(parts) == 3:
        dongle_id = parts[1]
        segment_name = parts[2]
        route = f"{dongle_id}|{segment_name}"
        try:
            end_ms = int(time.time() * 1000) + 86_400_000
            api_url = f"https://api.comma.ai/v1/devices/{dongle_id}/routes_segments?end={end_ms}&start=0"
            headers = {"Authorization": f"JWT {jwt_token}"} if jwt_token else {}
            resp = http_requests.get(api_url, headers=headers, timeout=10)
            if resp.status_code != 200:
                return None
            for r in resp.json():
                if r.get("fullname") == route:
                    return max(1, (r["end_time_utc_millis"] - r["start_time_utc_millis"]) // 1000)
        except Exception:
            return None

    return None


@app.post("/api/estimate")
async def estimate(body: EstimateRequest) -> dict[str, Any]:
    """Estimate output file size and route duration without starting a render."""
    if body.download_source == "ssh":
        return {"duration_seconds": None, "estimated_mb": None, "bitrate_kbps": None, "note": "Duration unknown for SSH routes"}

    duration = await asyncio.get_event_loop().run_in_executor(
        None, _resolve_route_duration, body.route, body.jwt_token
    )
    if duration is None:
        return {"duration_seconds": None, "estimated_mb": None, "bitrate_kbps": None}

    if body.file_size_mb <= 0:
        approx_kbps = _MAX_QUALITY_BITRATE_KBPS.get(body.render_type, 25_000)
        approx_mb = round(approx_kbps * duration / 8 / 1024, 1)
        return {
            "duration_seconds": duration,
            "estimated_mb": approx_mb,
            "bitrate_kbps": approx_kbps,
            "note": f"~{approx_mb} MB (estimated for max quality)",
        }

    bitrate_bps = body.file_size_mb * 8 * 1024 * 1024 // duration
    bitrate_kbps = round(bitrate_bps / 1000, 1)
    return {
        "duration_seconds": duration,
        "estimated_mb": body.file_size_mb,
        "bitrate_kbps": bitrate_kbps,
    }


@app.post("/api/clip", response_model=JobResponse)
async def create_clip(body: ClipRequestBody) -> dict[str, Any]:
    # Validate inputs
    if body.render_type not in VALID_RENDER_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown render type '{body.render_type}'. Valid: {', '.join(sorted(VALID_RENDER_TYPES))}",
        )

    route = body.route.strip()
    if not route:
        raise HTTPException(status_code=422, detail="Route URL is required.")

    if not (route.startswith("https://connect.comma.ai/") or "|" in route):
        raise HTTPException(
            status_code=422,
            detail="Route must be a connect.comma.ai URL or a pipe-delimited route ID (e.g. dongle|route).",
        )

    if body.file_size_mb < 0:
        raise HTTPException(status_code=422, detail="File size must be a positive number, or 0 for no limit.")

    if body.download_source not in ("connect", "ssh"):
        raise HTTPException(status_code=422, detail="Download source must be 'connect' or 'ssh'.")

    if body.download_source == "ssh" and not body.device_ip.strip():
        raise HTTPException(status_code=422, detail="Device IP address is required for SSH downloads.")

    job_id = uuid.uuid4().hex[:12]
    job = Job(job_id=job_id)
    JOBS[job_id] = job

    asyncio.create_task(_run_clip(job, body))

    return {"job_id": job_id, "state": job.state.value}


@app.get("/api/clip/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "job_id": job.job_id,
        "state": job.state.value,
        "error": job.error,
        "has_output": bool(job.output_path),
    }


@app.get("/api/clip/{job_id}/status")
async def stream_status(job_id: str) -> StreamingResponse:
    """SSE endpoint that streams job logs in real-time."""
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_stream():
        sent = 0
        last_progress = {}
        while True:
            while sent < len(job.logs):
                line = job.logs[sent]
                yield f"data: {line}\n\n"
                sent += 1

            if job.progress and job.progress != last_progress:
                last_progress = dict(job.progress)
                yield f"event: progress\ndata: {json.dumps(last_progress)}\n\n"

            if job.state in (JobState.done, JobState.failed):
                yield f"event: state\ndata: {job.state.value}\n\n"
                break

            await asyncio.sleep(0.3)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/api/clip/{job_id}/download")
async def download_clip(job_id: str) -> FileResponse:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.state != JobState.done or not job.output_path:
        raise HTTPException(status_code=400, detail="Clip not ready")
    return FileResponse(
        job.output_path,
        media_type="video/mp4",
        filename=f"clip-{job_id}.mp4",
    )


@app.get("/api/clip/{job_id}/host-path")
async def clip_host_path(job_id: str) -> dict[str, str]:
    """Return the filesystem path where the rendered clip lives."""
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.state != JobState.done or not job.output_path:
        raise HTTPException(status_code=400, detail="Clip not ready")
    job_dir = str(OUTPUT_DIR / job.job_id)
    return {"path": job.output_path, "folder": job_dir}


@app.get("/api/clip/{job_id}/open-file")
async def open_clip_file(job_id: str) -> dict[str, str]:
    """Open the rendered clip in the system default video player."""
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.state != JobState.done or not job.output_path:
        raise HTTPException(status_code=400, detail="Clip not ready")
    import subprocess
    subprocess.Popen(["xdg-open", job.output_path])
    return {"status": "ok"}


@app.get("/api/clip/{job_id}/open-folder")
async def open_clip_folder(job_id: str) -> dict[str, str]:
    """Open the output folder in the system file manager."""
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.state != JobState.done or not job.output_path:
        raise HTTPException(status_code=400, detail="Clip not ready")
    import subprocess
    subprocess.Popen(["xdg-open", str(Path(job.output_path).parent)])
    return {"status": "ok"}
