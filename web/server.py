"""FastAPI backend for the local Docker-based op-replay-clipper web UI."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
import struct
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
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

CLIPPER_IMAGE = os.environ.get("CLIPPER_IMAGE", "op-replay-clipper-render")
# Host path used for `docker run -v` mounts (must be a real host filesystem path).
SHARED_HOST_DIR = os.environ.get("SHARED_HOST_DIR", os.environ.get("SHARED_DIR", "/app/shared"))
# Local path inside the web container where the same volume is mounted.
SHARED_LOCAL_DIR = Path(os.environ.get("SHARED_LOCAL_DIR", "/app/shared"))
# Host home directory for mounting SSH keys (must be a real host path).
HOST_HOME_DIR = os.environ.get("HOST_HOME_DIR", str(Path.home()))
# Whether the host has an NVIDIA GPU (set to "false" by the desktop app on macOS).
HAS_GPU = os.environ.get("HAS_GPU", "true").lower() != "false"

VALID_RENDER_TYPES = {
    "ui", "ui-alt", "driver-debug", "forward", "wide",
    "driver", "360", "forward_upon_wide", "360_forward_upon_wide",
}
SMEAR_RENDER_TYPES = {"ui", "ui-alt", "driver-debug"}


# ---------------------------------------------------------------------------
# Job tracking
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
# Request / Response models
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
# Docker helpers
# ---------------------------------------------------------------------------

async def _docker_image_exists(image: str) -> bool:
    """Return True if the render Docker image is available locally."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "image", "inspect", image,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return (await proc.wait()) == 0
    except FileNotFoundError:
        return False


def _build_docker_cmd(job: Job, req: ClipRequestBody) -> list[str]:
    """Build the ``docker run`` command to execute clip.py inside the render container."""
    job_dir = SHARED_LOCAL_DIR / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    output_inside = f"/src/shared/{job.job_id}/output.mp4"
    is_ssh = req.download_source == "ssh"

    cmd: list[str] = [
        "docker", "run", "--rm",
        "--shm-size=1g",
    ]

    if HAS_GPU:
        cmd.extend(["--gpus", "all", "-e", "NVIDIA_DRIVER_CAPABILITIES=all"])

    cmd.extend(["-v", f"{SHARED_HOST_DIR}:/src/shared"])

    if is_ssh:
        # Host networking so the container can reach the device on the LAN.
        cmd.extend(["--network", "host"])
        # Mount the host SSH directory so the container can authenticate.
        host_ssh_dir = Path(HOST_HOME_DIR) / ".ssh"
        cmd.extend(["-v", f"{host_ssh_dir}:/root/.ssh:ro"])

    cmd.extend([
        CLIPPER_IMAGE,
        req.render_type,
        req.route,
        "-o", output_inside,
        "-m", str(req.file_size_mb),
        "--file-format", req.file_format,
    ])

    if req.render_type in SMEAR_RENDER_TYPES:
        cmd.extend(["--smear-seconds", str(req.smear_seconds)])

    if req.jwt_token and not is_ssh:
        cmd.extend(["-j", req.jwt_token])

    if is_ssh:
        cmd.extend(["--download-source", "ssh", "--device-ip", req.device_ip, "--ssh-port", str(req.ssh_port)])

    return cmd


async def _run_container(job: Job, req: ClipRequestBody) -> None:
    """Run the render container and stream its output into the job log."""
    try:
        cmd = _build_docker_cmd(job, req)
        job.state = JobState.running
        job.logs.append(f"$ {' '.join(cmd[:6])} ... {cmd[-1]}")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        assert proc.stdout is not None
        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
            job.logs.append(line)

            # Parse ffmpeg progress lines
            if line.startswith("frame="):
                m = _FFMPEG_PROGRESS_RE.match(line)
                if m:
                    progress: dict[str, Any] = {}
                    if m.group(3):
                        progress["size_kb"] = int(m.group(3))
                    if m.group(4):
                        progress["time_seconds"] = round(_parse_ffmpeg_time(m.group(4)), 1)
                    if m.group(5):
                        progress["bitrate_kbps"] = float(m.group(5))
                    if m.group(6):
                        progress["speed"] = float(m.group(6))
                    if progress:
                        job.progress = progress

        exit_code = await proc.wait()

        output_path = SHARED_LOCAL_DIR / job.job_id / "output.mp4"
        # Brief delay for filesystem sync after container exits
        await asyncio.sleep(1)
        file_found = output_path.exists()
        file_size = output_path.stat().st_size if file_found else 0
        if exit_code == 0 and file_found and file_size > 0:
            job.state = JobState.done
            job.output_path = str(output_path)
            job.logs.append("Render complete.")
        else:
            job.state = JobState.failed
            job.error = f"Container exited with code {exit_code}"
            job.logs.append(f"ERROR: {job.error}")
            job.logs.append(f"DEBUG: output_path={output_path}, exists={file_found}, size={file_size}")
            # List what's actually in the job directory for debugging
            job_dir = SHARED_LOCAL_DIR / job.job_id
            if job_dir.exists():
                contents = list(job_dir.iterdir())
                job.logs.append(f"DEBUG: job_dir contents={[f.name for f in contents]}")
    except FileNotFoundError:
        job.state = JobState.failed
        job.error = "Docker CLI not found. Is Docker installed?"
        job.logs.append(f"ERROR: {job.error}")
    except Exception as exc:
        job.state = JobState.failed
        job.error = str(exc)
        job.logs.append(f"ERROR: {job.error}")


# ---------------------------------------------------------------------------
# Device discovery helpers
# ---------------------------------------------------------------------------

_SCAN_PORTS = [8022, 22]  # comma 3X on 8022, comma 4 on 22
_SCAN_TIMEOUT = 1.0
_DEVICE_TYPE = {8022: "comma 3X", 22: "comma 4"}


def _detect_subnets() -> list[str]:
    """Detect LAN subnets to scan. Returns list of /24 base IPs like '192.168.1.'."""
    bases: set[str] = set()

    # Docker Desktop (macOS/Windows): resolve host.docker.internal
    try:
        host_ip = socket.gethostbyname("host.docker.internal")
        prefix = host_ip.rsplit(".", 1)[0] + "."
        bases.add(prefix)
    except socket.gaierror:
        pass

    # Linux: parse /proc/net/route for default gateway
    try:
        with open("/proc/net/route") as f:
            for line in f:
                fields = line.strip().split()
                if len(fields) >= 3 and fields[1] == "00000000":
                    # Default route — gateway is in fields[2] as hex
                    gw_hex = fields[2]
                    gw_bytes = struct.pack("<I", int(gw_hex, 16))
                    gw_ip = socket.inet_ntoa(gw_bytes)
                    prefix = gw_ip.rsplit(".", 1)[0] + "."
                    bases.add(prefix)
                    break
    except (FileNotFoundError, ValueError):
        pass

    # Also check the container's own IP interfaces
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and not ip.startswith("172."):
                prefix = ip.rsplit(".", 1)[0] + "."
                bases.add(prefix)
    except socket.gaierror:
        pass

    return list(bases) if bases else []


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


def _scan_subnet(base: str) -> list[dict[str, Any]]:
    """Scan a /24 subnet for SSH services on comma device ports."""
    results: list[dict[str, Any]] = []

    def probe(args: tuple[str, int]) -> dict[str, Any] | None:
        return _check_port(*args)

    targets = [(f"{base}{i}", port) for i in range(1, 255) for port in _SCAN_PORTS]

    with ThreadPoolExecutor(max_workers=64) as pool:
        for result in pool.map(probe, targets):
            if result is not None:
                results.append(result)

    return results


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html_path = Path(__file__).parent / "static" / "index.html"
    return HTMLResponse(content=html_path.read_text())


@app.get("/api/health")
async def health() -> dict[str, Any]:
    docker_ok = shutil.which("docker") is not None
    image_ok = await _docker_image_exists(CLIPPER_IMAGE) if docker_ok else False
    return {
        "docker": docker_ok,
        "image": image_ok,
        "image_name": CLIPPER_IMAGE,
    }


@app.post("/api/scan-devices")
async def scan_devices() -> dict[str, Any]:
    """Scan the local network for comma devices with SSH enabled."""
    loop = asyncio.get_event_loop()
    subnets = await loop.run_in_executor(None, _detect_subnets)
    if not subnets:
        return {"devices": [], "subnets_scanned": [], "error": "Could not detect local network subnet."}

    devices: list[dict[str, Any]] = []
    for base in subnets:
        found = await loop.run_in_executor(None, _scan_subnet, base)
        devices.extend(found)

    # Deduplicate by ip+port
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for d in devices:
        key = f"{d['ip']}:{d['port']}"
        if key not in seen:
            seen.add(key)
            unique.append(d)

    return {"devices": unique, "subnets_scanned": subnets}


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
# Based on typical openpilot video content characteristics.
_MAX_QUALITY_BITRATE_KBPS: dict[str, int] = {
    "ui": 40_000,            # 2160x2662 @ 20fps
    "ui-alt": 40_000,
    "driver-debug": 30_000,  # 1920x1720 @ 20fps
    "forward": 20_000,       # 1344x760 @ 20fps
    "wide": 20_000,
    "driver": 15_000,
    "360": 35_000,           # stitched wide+driver
    "forward_upon_wide": 22_000,
    "360_forward_upon_wide": 60_000,  # 8K output
}


def _resolve_route_duration(route_url: str, jwt_token: str = "") -> int | None:
    """Resolve the duration in seconds from a route URL. Returns None on failure."""
    route_url = route_url.strip()

    # Pipe-delimited route with no timing — can't resolve without API context
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

    if not await _docker_image_exists(CLIPPER_IMAGE):
        raise HTTPException(
            status_code=503,
            detail=f"Render image '{CLIPPER_IMAGE}' not found. Run './install.sh' or 'make docker-build' first.",
        )

    job_id = uuid.uuid4().hex[:12]
    job = Job(job_id=job_id)
    JOBS[job_id] = job

    asyncio.create_task(_run_container(job, body))

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

            # Emit progress updates if changed
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
    """Return the host filesystem path where the rendered clip lives."""
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.state != JobState.done or not job.output_path:
        raise HTTPException(status_code=400, detail="Clip not ready")
    host_path = f"{SHARED_HOST_DIR}/{job_id}/output.mp4"
    return {"path": host_path, "folder": f"{SHARED_HOST_DIR}/{job_id}"}
