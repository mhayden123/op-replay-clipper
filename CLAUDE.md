# GlideKit -- Project Reference

> Reference document for Claude Code sessions. Describes architecture,
> build process, API contracts, and platform-specific details.

---

## Architecture

```
Desktop App or Browser
        |
        v
   FastAPI Server (port 7860)
   - web/server.py: API endpoints, job management
   - Spawns clip.py as subprocess per render job
   - Streams stdout/stderr for real-time progress
        |
        v
   clip.py -> core/ -> renderers/
   - Runs natively with host GPU
   - openpilot built locally (for UI render types)
   - FFmpeg for encoding (NVENC or libx264)
        |
        v
   Output to ~/.glidekit/output/
```

---

## The openpilot Replay Binary

UI rendering depends on openpilot's native `replay` tool (C++ binary).

**What it does:**
- Replays logged driving data by publishing cereal messages
- Feeds data into openpilot's UI process which renders the visual overlay
- The clipper captures the rendered UI output via screen recording (Xvfb + ffmpeg x11grab)

**Key flags:**
- `--demo` -- built-in demo route
- `--data_dir` -- point at locally-stored route data
- `--dcam` / `--ecam` -- load driver/wide camera
- `--no-hw-decoder` -- software fallback
- `--no-loop` -- stop at end of route
- `--start` -- start from N seconds in
- `--playback` -- playback speed multiplier

**Build requirements:**
- C++ compiler, cmake, ncurses (vendored)
- cereal (Cap'n Proto messaging)
- OpenGL/EGL for UI rendering
- **NOT Qt** -- uses vendored ncurses for TUI

---

## What install.sh Does (Detailed Breakdown)

### Step 1: System Packages (apt/brew)
```
build-essential cmake jq ffmpeg eatmydata htop mesa-utils bc net-tools
wget curl capnproto git-lfs tzdata zstd git xserver-xorg-core
libxrandr-dev libxinerama-dev libxcursor-dev libxi-dev libxext-dev
libegl1-mesa-dev xorg-dev ca-certificates libcurl4-openssl-dev locales xvfb
```
**Note:** `xserver-xorg-video-nvidia-525` is deliberately excluded -- it's for
container environments and conflicts with host NVIDIA drivers.

### Step 2: Clone openpilot (shallow, master HEAD)
- Shallow partial clone: `--depth 1 --filter=blob:none --recurse-submodules`
- Destination: `~/.glidekit/openpilot/`

### Step 3: Install openpilot Dependencies
Calls openpilot's `tools/setup_dependencies.sh` which runs `uv sync --frozen --all-extras`.

### Step 4: Fix Vendored Tool Permissions
chmod +x on vendored binaries: `arm-none-eabi-*`, `capnp`, `capnpc*`, `ffmpeg`, `ffprobe`.

### Step 5: Build Native Clip Dependencies (scons -- TARGETED)
**NOT a full openpilot build.** Only 5 specific `.so` files:
```
msgq_repo/msgq/ipc_pyx.so
msgq_repo/msgq/visionipc/visionipc_pyx.so
common/params_pyx.so
selfdrive/controls/lib/longitudinal_mpc_lib/c_generated_code/acados_ocp_solver_pyx.so
selfdrive/controls/lib/lateral_mpc_lib/c_generated_code/acados_ocp_solver_pyx.so
```

### Step 6: Build Patched pyray (THE COMPLEX PART)
Patches GLFW to add EGL pbuffer support for headless GPU rendering:
1. Clone comma's raylib fork, patch for null platform + EGL pbuffer
2. Build `libraylib.a` with cmake
3. Clone comma's raylib-python-cffi fork, patch to link static lib + -lEGL
4. Build wheel and install into openpilot's venv

The `OPENPILOT_UI_NULL_EGL` env var enables headless rendering.

**macOS note:** EGL patches are Linux-specific. macOS uses CGL/Metal -- stock
pyray may work without patches. Needs validation.

### Step 7: Generate UI Font Atlases
`selfdrive/assets/fonts/process.py` generates bitmap font textures.

---

## clip.py Architecture

`clip.py` is the main entry point. Key details:

**CLI flags for native execution:**
- `--openpilot-dir` -- where openpilot is checked out
- `--skip-openpilot-update` / `--skip-openpilot-bootstrap` -- skip git/build
- `--data-root` / `--data-dir` -- downloaded route data location
- `--accel` -- `auto`, `cpu`, `videotoolbox`, `nvidia`
- `--headless` / `--windowed`
- `--download-source` -- `connect` (cloud) or `ssh` (direct from device)

**openpilot imports are conditional:**
Non-UI render types (forward, wide, driver, 360) work without openpilot.
UI types (ui, ui-alt, driver-debug, forward_upon_wide, 360_forward_upon_wide)
import openpilot modules and fail with a clear message if unavailable.

---

## server.py API Contracts

### Endpoints
| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/clip` | Start a render job |
| `GET` | `/api/clip/{job_id}` | Get job status |
| `GET` | `/api/clip/{job_id}/status` | SSE stream of logs + progress |
| `GET` | `/api/clip/{job_id}/download` | Download finished clip |
| `GET` | `/api/clip/{job_id}/host-path` | Get filesystem path of output |
| `GET` | `/api/clip/{job_id}/open-file` | Open clip in system video player |
| `GET` | `/api/clip/{job_id}/open-folder` | Open output folder in file manager |
| `POST` | `/api/test-ssh` | Test SSH connectivity to comma device |
| `POST` | `/api/estimate` | Estimate output file size and duration |
| `GET` | `/api/health` | Environment status |
| `GET` | `/api/platform` | Available render types per platform |
| `GET` | `/api/wsl/status` | WSL installation status (Windows) |
| `POST` | `/api/wsl/install` | Install WSL (Windows, triggers UAC) |
| `POST` | `/api/wsl/setup-clipper` | SSE-streamed clipper install in WSL |
| `GET` | `/` | Serve web UI |

### ClipRequestBody Schema
```python
route: str              # Comma Connect URL or pipe-delimited route ID
render_type: str = "ui"
file_size_mb: int = 9   # 0 = no limit
file_format: str = "auto"
smear_seconds: int = 3
jwt_token: str = ""
download_source: str = "connect"  # connect or ssh
device_ip: str = ""
ssh_port: int = 22
```

---

## Cross-Platform Details

### Windows
- Non-UI renders run natively (Python + FFmpeg only)
- UI renders invoke WSL: `wsl.exe -d Ubuntu -- bash -c "cd ~/glidekit && uv run python clip.py ..."`
- Output paths converted from Windows to /mnt/c/ for WSL access
- subprocess.Popen used instead of asyncio (ProactorEventLoop pipe reading is unreliable)
- Raw chunk reading (4KB) with \r/\n splitting for ffmpeg progress
- Registry key `HKCU\Software\GlideKit` tracks install paths

### macOS
- VideoToolbox for hardware acceleration
- `open` instead of `xdg-open` for file/folder actions
- pyray EGL patches are Linux-specific -- stock pyray may work via CGL

### Linux
- Full support for all render types
- NVIDIA NVENC for GPU encoding
- `xdg-open` for file/folder actions
- Xvfb for headless UI rendering

---

## Gotchas

1. **scons targets are specific** -- do NOT run scons without specifying the 5 targets.
   A full openpilot build takes hours and isn't needed.

2. **Patched pyray is critical** -- normal `pip install pyray` won't work for headless
   rendering. The EGL pbuffer patches enable `OPENPILOT_UI_NULL_EGL=1`.

3. **Two separate venvs** -- openpilot's venv (`~/.glidekit/openpilot/.venv/`)
   is separate from the clipper's venv (`./venv/`). The pyray wheel, scons .so files,
   and fonts live in openpilot's venv. Don't mix them.

4. **xserver-xorg-video-nvidia-525 must NOT be installed** -- it provides the X11 NVIDIA
   driver for container environments and conflicts with host drivers.

5. **openpilot's setup_dependencies.sh uses --all-extras** -- installs dev/test deps
   we don't need. Changing it could break scons or font generation.

6. **Partial clone fetches on demand** -- the `--filter=blob:none` clone may fetch
   additional objects during build steps. Network must be available.

7. **Windows subprocess buffering** -- Python buffers stdout when piped. Use `-u` flag
   and `PYTHONUNBUFFERED=1`. Even then, asyncio ProactorEventLoop is unreliable for
   pipe reading -- use synchronous Popen in a thread instead.

8. **WSL check can hang** -- `wsl.exe --list --verbose` hangs for 30+ seconds on
   machines without WSL. Always check `wsl.exe` existence first and use a 5-second timeout.
