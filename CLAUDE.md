# OP Replay Clipper — Native Migration Plan

> **Purpose:** Reference document for Claude Code sessions. This is the authoritative guide
> for migrating the op-replay-clipper rendering pipeline from a Docker-based architecture
> to a fully native, local execution model.

---

## Background & Motivation

The upstream project (`nelsonjchen/op-replay-clipper`) currently runs in Docker containers
with NVIDIA GPU passthrough. The original creator has validated a dockerless approach on
macOS and wants the project to move fully native. This migration is being developed
independently in a new repo to avoid disrupting the original codebase.

**Why native?**
- Docker + NVIDIA Container Toolkit + Compose V2 is a heavy prereq stack for end users
- Container-in-container spawning (web container → render container via Docker socket) adds unnecessary complexity for a local tool
- Native GPU access is simpler — if `nvidia-smi` works, you're good
- Removes friction on immutable/atomic distros (e.g., Bazzite/Fedora Atomic where Docker installation is non-trivial)

---

## Source Repos (Reference Only — Do Not Modify)

- **Core pipeline (fork):** `https://github.com/mhayden123/op-replay-clipper`
- **Desktop app:** `https://github.com/mhayden123/op-replay-clipper-desktop`
- **Upstream original:** `https://github.com/nelsonjchen/op-replay-clipper`
- **openpilot (replay binary):** `https://github.com/commaai/openpilot` — specifically `tools/replay/main.cc`

---

## Upstream vs. Fork Architecture Differences

Understanding how the fork diverged from upstream is important for the migration.

**Upstream (nelsonjchen):**
- Shell-based orchestration: `clip.sh` is the main driver
- `downloader.py` + `ffmpeg_clip.py` + `predict.py` (Replicate/Cog focused)
- ~62% Python, ~33% Shell
- Deployed on Replicate as a pay-as-you-go cloud service
- Simpler structure, single Dockerfile

**Fork (mhayden123):**
- Refactored into structured Python: `clip.py` → `core/` → `renderers/`
- ~83% Python, much less shell
- Added: Web UI (FastAPI), Desktop app, Docker Compose orchestration
- Added: Local SSH download source (download directly from comma device on LAN)
- Added render types: `ui-alt`, `driver-debug`, `fwd-wide`, `360-fwd-wide`
- Two-container Docker model (web + render)

**Implication:** The fork's Python refactor is actually cleaner for native migration
than the upstream's shell-heavy approach. The structured `core/` + `renderers/`
architecture should port well — the Docker removal is mostly about the orchestration
layer, not the rendering pipeline itself.

---

## The openpilot Replay Binary

The UI rendering pipeline depends on openpilot's native `replay` tool, which is
a compiled C++ binary. Understanding this is critical for the native build.

**What it does:**
- Replays logged driving data by publishing cereal messages
- Feeds data into openpilot's UI process which renders the visual overlay
- The clipper captures the rendered UI output via screen recording (Xvfb + ffmpeg x11grab)

**Key technical details from `tools/replay/main.cc`:**
- Standard C++ with POSIX `getopt_long` for CLI parsing — **NOT Qt-based** (earlier
  assumption was wrong). The TUI uses vendored ncurses, not Qt.
- Includes: `common/prefix.h`, `common/timing.h`, `tools/replay/consoleui.h`,
  `tools/replay/replay.h`, `tools/replay/util.h`
- The `Replay` class constructor takes: route, allow/block service lists, flags,
  data_dir, and auto_source — the `--data_dir` flag maps directly to a local
  filesystem path, which is exactly what the SSH download source needs.
- Has explicit macOS support (`#ifdef __APPLE__` for file descriptor limits)
- Ships a vendored ncurses static library (with a workaround for terminfo paths)

**Flags relevant to the clipper:**
- `--demo` — built-in demo route, perfect for Phase 0 validation
- `--data_dir` — point at locally-stored route data (key for SSH downloads)
- `--dcam` / `--ecam` — load driver/wide camera (needed for 360 and driver-debug renders)
- `--no-hw-decoder` — software fallback if HW decode isn't available
- `--no-loop` — stop at end of route (important for clip rendering)
- `--no-vipc` — skip video output (useful for data-only replay)
- `--benchmark` — process all events then exit with timing stats (useful for perf testing)
- `--start` — start from N seconds in (used for smear/preroll logic)
- `--playback` — playback speed multiplier

**Build requirements:**
- C++ compiler (g++ or clang++)
- ncurses (vendored, but may need system ncurses-dev for headers)
- cereal (Cap'n Proto-based messaging — openpilot's IPC layer)
- Video decoding libraries (FFmpeg/libav for software decode, plus HW decode support)
- OpenGL/EGL for the UI rendering process (separate from replay, but launched alongside)
- **NOT Qt** — this simplifies the native dependency chain significantly

**openpilot's own platform targets:**
- Primary dev target: **Ubuntu 24.04**
- "Most of openpilot should work natively on macOS" — confirmed by `#ifdef __APPLE__` in code
- The Docker image's Ubuntu 22.04 base may be behind openpilot's current expectations

---

## What bootstrap_image_env.sh Actually Does (Detailed Breakdown)

This is the single most important file for the migration. Here's exactly what it does,
step by step, and what native equivalents are needed.

### Step 1: Install System Packages (apt)
```
build-essential cmake jq ffmpeg faketime eatmydata htop mesa-utils bc net-tools
sudo wget curl capnproto git-lfs tzdata zstd git xserver-xorg-core
xserver-xorg-video-nvidia-525 libxrandr-dev libxinerama-dev libxcursor-dev
libxi-dev libxext-dev libegl1-mesa-dev xorg-dev
```
**Native migration note:** Most of these install directly on Mint/Ubuntu. The
`xserver-xorg-video-nvidia-525` package is Docker-specific (provides X server nvidia
driver inside the container) — on a native install the host NVIDIA driver handles this.
Replace with whatever nvidia driver package the distro provides.

### Step 2: Clone openpilot (shallow, master HEAD)
- Clones from `https://github.com/commaai/openpilot.git`
- Branch: `master` (configurable via `OPENPILOT_BRANCH` env var)
- Depth: 1, with `--filter=blob:none --recurse-submodules --shallow-submodules`
- Destination: `$OPENPILOT_ROOT` (default: `/home/batman/openpilot`)
- **Native migration:** Change default to `~/.op-replay-clipper/openpilot/`

### Step 3: Install openpilot Dependencies
Calls openpilot's own `tools/setup_dependencies.sh` which:
- Installs a small set of apt packages: `ca-certificates build-essential curl
  libcurl4-openssl-dev locales git xvfb`
- Sets up udev rules (for panda/jungle devices — not needed for clipper)
- Installs uv if not present
- Runs `uv sync --frozen --all-extras` in the openpilot directory
- Supports Ubuntu jammy (22.04), kinetic (22.10), and noble (24.04)

### Step 4: Fix Vendored Tool Permissions
Chmod +x on vendored binaries in `.venv/lib`: `arm-none-eabi-*`, `capnp`, `capnpc*`,
`ffmpeg`, `ffprobe`. These are tools bundled in openpilot's Python environment.

### Step 5: Build Native Clip Dependencies (scons — TARGETED, NOT FULL BUILD)
**This is NOT a full openpilot build.** It builds only 5 specific `.so` files:
```
scons -j$(nproc) \
  msgq_repo/msgq/ipc_pyx.so \
  msgq_repo/msgq/visionipc/visionipc_pyx.so \
  common/params_pyx.so \
  selfdrive/controls/lib/longitudinal_mpc_lib/c_generated_code/acados_ocp_solver_pyx.so \
  selfdrive/controls/lib/lateral_mpc_lib/c_generated_code/acados_ocp_solver_pyx.so
```
These are Cython extensions for IPC, vision IPC, params, and MPC solvers. The scons
build compiles C/C++ code and links against system libraries. This is much faster
and simpler than building all of openpilot.

### Step 6: Build Patched pyray (THE COMPLEX PART)
⚠️ **This is the most intricate step.** It:
1. Clones comma's fork of raylib (`https://github.com/commaai/raylib.git`)
2. Patches GLFW source code to add EGL pbuffer support for headless rendering:
   - Adds `EGL_PBUFFER_BIT` constant
   - Adds `eglCreatePbufferSurface` function pointer and loader
   - Adds null platform selector triggered by `OPENPILOT_UI_NULL_EGL` env var
   - Patches `rcore_desktop_glfw.c` to use null platform when env var is set
   - Patches `egl_context.c` to create pbuffer surfaces instead of window surfaces
3. Builds raylib as a static library (`libraylib.a`) with cmake:
   - `PLATFORM=Desktop`, `GLFW_BUILD_WAYLAND=OFF`, `GLFW_BUILD_X11=ON`
   - `BUILD_SHARED_LIBS=OFF`, position-independent code enabled
4. Clones comma's fork of `raylib-python-cffi`
5. Patches `raylib/build.py` to link against the static `libraylib.a` and add `-lEGL`
6. Builds a wheel and installs it into openpilot's venv
7. Verifies the install checks for `libraylib.a` and `-lEGL` in the build config

**Why this matters:** This custom pyray is what enables headless GPU-accelerated
UI rendering. Without these patches, the UI renderer would need an X display.
The `OPENPILOT_UI_NULL_EGL` env var is the key — when set, GLFW uses a null
platform with EGL pbuffer surfaces instead of real windows.

### Step 7: Generate UI Font Atlases
Runs `selfdrive/assets/fonts/process.py` inside openpilot. This generates bitmap
font atlas textures that the UI renderer uses. Requires the openpilot venv to be
set up first.

### Step 8: Record Commit Hash
Writes `git rev-parse HEAD` to `$OPENPILOT_ROOT/COMMIT` for version tracking.

---

## clip.py Architecture (Already Docker-Agnostic)

`clip.py` is the main entry point and is **already designed for native execution**.
There is nothing Docker-specific in it. Key details:

**Imports:**
- `core.clip_orchestrator` — `ClipRequest`, `RenderType`, `run_clip()`
- `core.openpilot_bootstrap` — `bootstrap_openpilot()`, `ensure_openpilot_checkout()`
- `core.openpilot_config` — default paths and branch settings

**Key CLI flags for native execution:**
- `--openpilot-dir` — where openpilot is checked out (default from config)
- `--skip-openpilot-update` — don't git pull openpilot
- `--skip-openpilot-bootstrap` — don't rebuild openpilot deps
- `--data-root` — where downloaded route data lives (default: `./shared/data_dir`)
- `--data-dir` — explicit data directory override
- `--accel` — acceleration method: `auto`, `cpu`, `videotoolbox`, `nvidia`
- `--headless` / `--windowed` — headless is default
- `--download-source` — `connect` (cloud) or `ssh` (direct from device)
- `--device-ip` / `--ssh-port` — for SSH downloads (port 22 for comma 4, 8022 for 3X)
- `--skip-download` — reuse already-downloaded data

**Bootstrap flow in clip.py:**
1. If render type needs openpilot (UI renders), check if openpilot dir exists
2. If `--skip-openpilot-update` is NOT set, call `ensure_openpilot_checkout()` (git clone/pull)
3. If `--skip-openpilot-bootstrap` is NOT set, call `bootstrap_openpilot()` (deps/build)
4. Build a `ClipRequest` dataclass and pass to `run_clip()`

**Implication for migration:** clip.py doesn't need rewriting. The native install
script just needs to set up the environment so that `uv run python clip.py` works.

---

## server.py API Contracts (Must Preserve)

The web server (`web/server.py`) has these endpoints that the desktop app depends on:

### Endpoints
| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/clip` | Start a render job. Body: `ClipRequestBody` |
| `GET` | `/api/clip/{job_id}` | Get job status (state, error, has_output) |
| `GET` | `/api/clip/{job_id}/status` | SSE stream of real-time logs + progress |
| `GET` | `/api/clip/{job_id}/download` | Download finished clip as MP4 |
| `GET` | `/api/clip/{job_id}/host-path` | Get filesystem path of output |
| `POST` | `/api/test-ssh` | Test SSH connectivity to a comma device |
| `POST` | `/api/estimate` | Estimate output file size and duration |
| `GET` | `/` | Serve web UI HTML |

### ClipRequestBody Schema
```python
route: str              # Comma Connect URL or pipe-delimited route ID
render_type: str = "ui" # One of VALID_RENDER_TYPES
file_size_mb: int = 9   # Target file size (0 = no limit)
file_format: str = "auto"  # auto, h264, hevc
smear_seconds: int = 3  # Preroll for UI renders
jwt_token: str = ""     # For private routes
download_source: str = "connect"  # connect or ssh
device_ip: str = ""     # For SSH downloads
ssh_port: int = 22      # 22 for comma 4, 8022 for comma 3X
```

### What Changes in the Rewrite
The ONLY function that needs rewriting is `_build_docker_cmd()` → replace with
direct subprocess invocation of clip.py. The `_run_container()` function's streaming
logic (reading stdout line by line, parsing ffmpeg progress) stays almost identical —
just change `docker run ...` to `uv run python clip.py ...`.

**Key environment variables the current server uses (remove these):**
- `CLIPPER_IMAGE` — Docker image name (not needed)
- `SHARED_HOST_DIR` — host path for Docker volume mounts (not needed)
- `SHARED_LOCAL_DIR` — container path for shared volume (replace with local dir)
- `HOST_HOME_DIR` — for mounting SSH keys into container (not needed, host has them)
- `HAS_GPU` — for `--gpus all` Docker flag (detect natively instead)

```
Desktop App / Browser
        │
        ▼
   Web Container (FastAPI, port 7860)
   - Dockerfile.web: python:3.12-slim + Docker CLI
   - Spawns render containers via /var/run/docker.sock
   - Serves web UI, manages jobs
        │
        ▼ (spawns per-job via Docker socket)
   Render Container (NVIDIA GPU)
   - Dockerfile: nvidia/cuda:12.4.1-devel-ubuntu22.04
   - Python 3.12 via deadsnakes PPA
   - bootstrap_image_env.sh: system packages, openpilot clone,
     native dep builds, patched pyray, font generation
   - uv for Python dependency management
   - Entrypoint: uv run clip.py --skip-openpilot-update --skip-openpilot-bootstrap
        │
        ▼
   clip.py → core/ → renderers/
        │
        ▼
   Output to shared/ volume
```

### Key Docker Components to Replace
| Component | Docker Role | Native Replacement |
|---|---|---|
| `Dockerfile` | Reproducible render environment | Native install script (distro-aware) |
| `Dockerfile.web` | Web server + Docker CLI for spawning | FastAPI running directly, subprocess for clip.py |
| `docker-compose.yml` | Orchestration, GPU reservation, shared volumes | Single-process or simple service manager |
| `bootstrap_image_env.sh` | System deps, openpilot build, pyray, fonts | Same script adapted for host execution |
| NVIDIA Container Toolkit | GPU passthrough to containers | Direct GPU access (already present on host) |
| Docker socket mount | Web spawns render containers | Direct subprocess invocation |
| `shared/` volume | Inter-container file sharing | Local filesystem directory |

---

## Target Architecture (Native)

```
Desktop App / Browser
        │
        ▼
   FastAPI Server (runs natively, port 7860)
   - No Docker dependency
   - Invokes clip.py as subprocess (or in-process import)
   - Streams stdout/stderr for real-time progress
        │
        ▼
   clip.py → core/ → renderers/
   - Runs natively with host GPU
   - All deps installed on host (CUDA, openpilot, ffmpeg, pyray, etc.)
        │
        ▼
   Output to local directory (e.g., ~/.op-replay-clipper/output/)
```

---

## Files to Bring Forward from Original Repo

### MUST HAVE (core pipeline)
- `clip.py` — main entry point
- `core/` — rendering pipeline logic
- `renderers/` — render type implementations (UI, forward, wide, driver, 360, etc.)
- `common/` — shared utilities (includes bootstrap_image_env.sh to adapt)
- `patches/` — openpilot patches
- `pyproject.toml` + `uv.lock` — Python dependency definitions

### MUST HAVE (web layer — but will be rewritten)
- `web/` — FastAPI server (reference for API contracts, but Docker-spawning logic gets replaced)

### LEAVE BEHIND
- `Dockerfile`, `Dockerfile.web`, `Dockerfile.dockerignore`, `.dockerignore`
- `docker-compose.yml`
- `cog/`, `cog_predictor.py` — Replicate/Cog-specific (cloud inference)
- `replicate_run.py` — Replicate-specific
- `.devcontainer/` — VS Code devcontainer config
- `.github/workflows/` — review for relevance, but Docker-based CI will need reworking

---

## Migration Phases

### Phase 0: Validate Core Pipeline Natively (DO THIS FIRST)

**Goal:** Get `clip.py --demo` running on bare metal with GPU acceleration.

This is the litmus test. If this works, everything else is plumbing.

**Recommended Dev Environment:**

The bootstrap script and openpilot build chain target Ubuntu (apt-based). To avoid
fighting distro differences at the same time as removing Docker, use a Debian/Ubuntu
environment for Phase 0:

- **Primary (desktop, RTX 4090):** Use `distrobox` on Bazzite to get a mutable
  Ubuntu 22.04 shell with direct GPU passthrough. No Docker or NVIDIA Container
  Toolkit required. Create with:
  `distrobox create --name clipper-dev --image ubuntu:22.04 --nvidia`
  This gives full `apt-get` access and `nvidia-smi` visibility of the 4090 while
  staying on Bazzite as the host OS.

- **Secondary (laptop):** Linux Mint (or similar Ubuntu/Debian-based distro) installed
  natively. Keeps things as simple as possible — no immutable FS quirks, no rpm-ostree,
  just a standard mutable apt-based system. Good for iterating on the non-GPU parts
  of the pipeline and install script. GPU rendering validation should happen on the
  desktop with the 4090.

Once the pipeline is proven on Ubuntu/Debian, adapting the install script for
Fedora/Arch becomes a separate, well-scoped task in Phase 2.

**Steps:**
1. Set up dev environment (fresh Mint/Pop install on desktop)
2. Verify `nvidia-smi` works and shows RTX 5080
3. Install system packages from the bootstrap `APT_PACKAGES` list (see detailed
   breakdown above), MINUS `xserver-xorg-video-nvidia-525` (Docker-specific)
4. Install Python 3.12 and `uv`
5. Clone openpilot: `git clone --depth 1 --filter=blob:none --recurse-submodules --shallow-submodules https://github.com/commaai/openpilot.git`
6. Run openpilot's own `tools/setup_dependencies.sh` from inside the clone
7. Run the targeted scons build (the 5 specific .so targets)
8. Build the patched pyray (the embedded Python script from bootstrap)
9. Generate font atlases: `uv run --no-sync python selfdrive/assets/fonts/process.py`
10. Copy the clipper project files (clip.py, core/, renderers/, etc.) into the new repo
11. Run `uv sync` for the clipper's own dependencies
12. **Smoke test:** `uv run python clip.py forward --demo` (no UI, just video transcode)
13. **Full test:** `uv run python clip.py ui --demo` (full UI render with openpilot overlay)

**Critical Notes:**
- The openpilot native build is the hardest part. By using an apt-based environment
  for Phase 0, the bootstrap script should work with minimal changes — the goal is
  to validate the pipeline first, then worry about cross-distro support later.
- CUDA version: The Docker image uses CUDA 12.4.1 devel. The host needs compatible
  CUDA toolkit + drivers. Don't assume exact version match is required — test with
  what's installed and note minimum version.
- The `--skip-openpilot-update` and `--skip-openpilot-bootstrap` flags suggest clip.py
  has first-run bootstrap logic built in. Understand this flow before bypassing it.

---

### Phase 1: Collapse the Two-Container Architecture

**Goal:** Single-process server that runs clip.py directly.

**Steps:**
1. Replace `_build_docker_cmd()` in server.py with a function that builds:
   `["uv", "run", "python", "clip.py", render_type, route, "-o", output_path, ...]`
2. Replace `_run_container()` with `asyncio.create_subprocess_exec()` calling
   the clip.py command directly — keep the existing stdout streaming and ffmpeg
   progress parsing logic (it already works on raw text lines)
3. Remove `_docker_image_exists()` check from the `/api/clip` endpoint
4. Remove env vars: `CLIPPER_IMAGE`, `SHARED_HOST_DIR`, `HOST_HOME_DIR`
5. Replace `SHARED_LOCAL_DIR` with a configurable local output directory
   (default: `~/.op-replay-clipper/output/`)
6. For SSH downloads: remove Docker `--network host` and `-v .ssh` mount logic
   (not needed natively — clip.py already has direct SSH access)
7. Update `/api/clip/{id}/host-path` to return the native path
8. Update `/` to serve the web UI (the HTML file from `web/`)
9. Remove Docker CLI dependency from the server entirely

**API Contract Preservation:**
All endpoints (`POST /api/clip`, `GET /api/clip/{id}/status` SSE, `GET /api/clip/{id}/download`)
stay the same. The desktop app should not need any changes to talk to the new server.
The only visible difference is that jobs start faster (no container spin-up time).

---

### Phase 2: Native Install Script

**Goal:** One-command setup that works on supported Linux distros.

**Steps:**
1. Adapt `bootstrap_image_env.sh` into a host-native `install.sh`:
   - Detect distro (Ubuntu/Debian vs. Fedora/RHEL vs. Arch)
   - Install system packages via appropriate package manager
   - Validate CUDA toolkit + driver version
   - Clone openpilot to a managed location (e.g., `~/.op-replay-clipper/openpilot/`)
   - Build openpilot native dependencies
   - Install patched pyray
   - Generate fonts
2. Add prerequisite validation (check for nvidia-smi, CUDA version, Python version, etc.)
   with clear error messages
3. Make the install idempotent — safe to re-run for updates
4. Add an `uninstall.sh` or `--uninstall` flag for clean removal

**Distro Support Priority:**
1. Ubuntu 22.04+ (closest to original Docker base, easiest)
2. Fedora 38+ / Bazzite (Hayden's primary environment)
3. Arch (community interest, usually straightforward)

---

### Phase 3: Desktop App Adaptation

**Goal:** Desktop app works without Docker dependency.

**Steps:**
1. Remove Docker/container management from the desktop app
2. Option A: Desktop app launches FastAPI server as a managed child process
3. Option B: Desktop app talks directly to the rendering pipeline (skip web layer entirely)
4. Update installer/packaging to include native setup or guide user through it
5. Consider whether the desktop app should bundle its own Python environment or
   rely on system Python + uv

---

### Phase 4: Polish & Distribution

**Goal:** Smooth first-run experience for new users.

**Steps:**
1. First-run wizard or guided setup in the desktop app
2. Automatic dependency checking with actionable error messages
3. Update README and documentation for native-only workflow
4. Consider CI/CD: test native builds on Ubuntu and Fedora in GitHub Actions
   (no Docker-in-Docker needed anymore, which actually simplifies CI)
5. Release tagging and versioning strategy

---

## Key Technical Risks & Mitigations

### Risk: openpilot Build Fails on Non-Ubuntu
**Impact:** High — blocks the entire project on Fedora/Bazzite
**Mitigation:** Start with Ubuntu validation, then adapt. The bootstrap script
likely installs specific `-dev` packages that have different names on Fedora.
Create a package name mapping table. If openpilot's build system is too tightly
coupled to Ubuntu, consider maintaining a compatibility layer or building openpilot
components in an isolated environment (e.g., toolbox/distrobox as a lighter
alternative to full Docker — but only as a fallback).

### Risk: CUDA Version Mismatch
**Impact:** Medium — rendering fails or falls back to CPU
**Mitigation:** Don't hardcode CUDA 12.4.1. Detect installed CUDA version,
validate it meets minimum requirements, and let the host driver handle compatibility.
Document tested CUDA version ranges.

### Risk: macOS vs. Linux Divergence
**Impact:** Low-Medium — nelsonjchen's macOS solution may not translate
**Mitigation:** The macOS path likely uses CPU or Metal, not CUDA. Treat the
Linux native path as its own implementation. Share the Python/pipeline layer
but expect the GPU acceleration and system dependency layers to differ.

### Risk: Breaking the Desktop App
**Impact:** Medium — desktop app currently expects Docker-based API
**Mitigation:** Keep API contracts stable during migration. Phase the desktop
app changes separately after the core pipeline is validated.

---

## Environment Details

### Desktop (Primary — main development workspace)
- **OS:** TBD — recommend Ubuntu 24.04-based (Mint 22 or Pop!_OS) to align with
  openpilot's current dev target
- **GPU:** NVIDIA RTX 5080
- **Python Tooling:** uv
- **Coding Agent:** Claude Code

### Laptop (Secondary — mobile development)
- **Host OS:** Bazzite Linux (KDE Plasma, Fedora Atomic base)
- **Dev Environment:** Distrobox with Ubuntu 22.04 available if needed
- **GPU:** NVIDIA RTX 4090 Mobile
- **Python Tooling:** uv
- **Coding Agent:** Claude Code

---

## Open Questions

- [x] ~~What exact openpilot commit/tag does the current Docker build pin to?~~ — **Answered: master HEAD at shallow depth 1.** No specific commit pin. The bootstrap records the commit hash to `$OPENPILOT_ROOT/COMMIT` after cloning.
- [x] ~~Does bootstrap_image_env.sh compile CUDA kernels, or just link against CUDA libraries?~~ — **Answered: It does a TARGETED scons build** of 5 specific Cython `.so` files (msgq IPC, visionipc, params, MPC solvers). Not CUDA kernels. CUDA is needed at runtime for GPU-accelerated rendering but not compiled during bootstrap.
- [ ] Can openpilot's native deps be built with a newer CUDA than 12.4.1?
- [ ] Does nelsonjchen's macOS dockerless approach change any of the core pipeline APIs?
- [ ] Should the new repo be a monorepo (pipeline + desktop app) or stay split?
- [x] ~~Is there a minimum openpilot version requirement, or does it track HEAD?~~ — **Answered: Tracks master HEAD.** Shallow clone, no version pinning.
- [x] ~~What's the font generation step actually doing?~~ — **Answered:** Runs `selfdrive/assets/fonts/process.py` to generate bitmap font atlas textures for the UI renderer. Requires the openpilot venv. Could potentially be pre-built and cached.
- [ ] Should we target Ubuntu 24.04 instead of 22.04? (Likely yes — openpilot's `setup_dependencies.sh` supports jammy/kinetic/noble, and Mint 22 + Pop are 24.04-based)
- [x] ~~What Qt version does the replay binary need?~~ — **Answered: replay does NOT use Qt.** Uses POSIX getopt_long + vendored ncurses.
- [x] ~~Does the smear/preroll logic have Docker-specific assumptions?~~ — **Answered: No.** It's purely pipeline logic in clip.py via `--smear-seconds` flag, which controls how many seconds of data are replayed before the visible clip starts. Uses `--start` on the replay binary.
- [ ] Does the patched pyray wheel need to be rebuilt every time openpilot updates, or can it be cached independently?
- [ ] What happens when `OPENPILOT_UI_NULL_EGL` is set but no EGL-capable GPU driver is present? (Graceful error or silent failure?)

---

## ⚠️ GOTCHAS FOR CLAUDE CODE — READ BEFORE CODING

These are non-obvious traps and important context that an AI coding agent might
miss. **Read this section before starting any implementation work.**

### 1. clip.py is ALREADY native — don't rewrite it
`clip.py` has zero Docker dependencies. It uses `core.clip_orchestrator.run_clip()`
which is pure Python. The `--openpilot-dir`, `--accel`, `--headless`, and
`--download-source ssh` flags already support native execution. The migration
work is about the *environment setup* and *server.py*, not clip.py itself.

### 2. The pyray build is the single hardest step — don't skip or simplify it
The bootstrap builds a PATCHED version of raylib + pyray from comma's forks.
The patches add EGL pbuffer surface support for headless rendering. This is NOT
a normal `pip install pyray`. A normal pyray will NOT work for headless rendering.
The entire embedded Python script in `install_accelerated_linux_pyray()` must be
preserved or carefully adapted. Key env var: `OPENPILOT_UI_NULL_EGL=1`.

### 3. The scons build is TARGETED, not full openpilot
Only 5 `.so` files are built. Do NOT run `scons` without arguments (that builds
everything and takes forever). The exact targets are:
```
msgq_repo/msgq/ipc_pyx.so
msgq_repo/msgq/visionipc/visionipc_pyx.so
common/params_pyx.so
selfdrive/controls/lib/longitudinal_mpc_lib/c_generated_code/acados_ocp_solver_pyx.so
selfdrive/controls/lib/lateral_mpc_lib/c_generated_code/acados_ocp_solver_pyx.so
```

### 4. server.py rewrite is minimal — don't over-engineer it
The ONLY function that truly changes is `_build_docker_cmd()`. Replace it with
a function that builds a `uv run python clip.py ...` command list. The
`_run_container()` function's async stdout streaming and ffmpeg progress parsing
logic stays nearly identical — just point it at the subprocess instead of Docker.
Keep all API endpoints, SSE streaming, and job tracking as-is.

### 5. Don't change default paths without understanding the chain
- `clip.py --data-root` defaults to `./shared/data_dir` — route data goes here
- `clip.py --output` defaults to `./shared/local-clip.mp4`
- server.py uses `SHARED_LOCAL_DIR` for job output directories
- The desktop app uses `/api/clip/{id}/host-path` to find rendered clips
All of these need to be consistent in the native version. Recommend:
`~/.op-replay-clipper/output/` for rendered clips, `~/.op-replay-clipper/data/`
for downloaded route data.

### 6. The NVIDIA driver package in apt is Docker-specific
The bootstrap installs `xserver-xorg-video-nvidia-525`. This is for the Docker
container's X server. On a native install, the host NVIDIA driver is already
installed and provides this. Do NOT install this package natively — it can
conflict with the host driver. Remove it from the apt package list.

### 7. openpilot's setup_dependencies.sh runs `uv sync --all-extras`
This installs ALL optional Python dependencies for openpilot, which includes
dev tools, testing frameworks, etc. For the clipper, we might not need all-extras.
However, changing this could break assumptions in the scons build or font generation.
Safer to keep `--all-extras` initially and optimize later.

### 8. The openpilot clone uses `--filter=blob:none`
This is a partial clone (treeless). Git objects are downloaded on-demand. This
saves bandwidth and disk space but means the first build might fetch additional
blobs. Make sure the network is available during the bootstrap.

### 9. Two different Python environments are in play
- The CLIPPER's Python env: managed by the project's own `pyproject.toml` + `uv.lock`
- openpilot's Python env: its own `.venv/` with its own `pyproject.toml`
The patched pyray is installed into OPENPILOT's venv, not the clipper's.
The scons build runs inside openpilot's venv via `uv run --no-sync`.
Don't mix these up or install things into the wrong environment.

### 10. `--skip-openpilot-bootstrap` checks for `.venv/bin/python`
clip.py validates that openpilot is bootstrapped by checking if
`{openpilot_dir}/.venv/bin/python` exists. If you're debugging and the venv
is broken, this check will pass but the actual render will fail. When in doubt,
delete the `.venv` dir and re-bootstrap.

### 11. Font generation can fail silently
The `selfdrive/assets/fonts/process.py` script generates font atlases. If it
fails or is skipped, the UI renderer may render with missing or garbled text.
The fonts need to be regenerated if openpilot is updated. This step requires
the openpilot venv to be fully set up (after `uv sync` and scons build).

### 12. SSH downloads need host SSH keys — no longer mounted from Docker
The Docker server mounts `$HOST_HOME_DIR/.ssh` into the container. Natively,
the SSH keys are already in `~/.ssh/`. The `--device-ip` and `--ssh-port` flags
on clip.py handle the rest. Just make sure `~/.ssh/config` or known_hosts
has the comma device's key accepted.

### 13. The HAS_GPU detection needs to change
server.py currently reads `HAS_GPU` from an env var (set to "false" by the
desktop app on macOS for Docker `--gpus` flag). Natively, detect GPU presence
by checking if `nvidia-smi` succeeds, not via env var. clip.py already has
`--accel auto` which handles this for the render pipeline, but server.py may
need updating for any GPU-specific logic.

### 14. faketime package is used but may not be needed natively
The bootstrap installs `faketime` (libfaketime) — this is used to manipulate
system time for testing. It may be a leftover from the Docker/Replicate
environment. Verify if the clipper actually uses it before including it in
the native install.

---

## Notes & Scratchpad

_Use this section to capture decisions, discoveries, and gotchas as work progresses._

- nelsonjchen has confirmed dockerless works on macOS (April 2026)
- Docker removal aligns with simplifying the user prereq stack
- New repo will be created fresh — not a fork — for clean separation
- The `--skip-openpilot-update` and `--skip-openpilot-bootstrap` CLI flags are
  important — they suggest clip.py already handles both first-run and subsequent-run
  paths, which is a solid foundation for the native installer to build on
- **Dev environment decision:** Use distrobox (Ubuntu 22.04) on Bazzite desktop for
  GPU work, Linux Mint on laptop for general iteration. Solve "remove Docker" and
  "cross-distro support" as separate problems — don't fight both at once.
  Fedora/Arch adaptation comes in Phase 2 after the pipeline is proven on apt-based systems.
- **openpilot now targets Ubuntu 24.04** as its primary dev platform (not 22.04).
  The Docker image's Ubuntu 22.04 base is potentially behind. Consider targeting
  24.04 for the native install — Mint 22 and Pop!_OS are both 24.04-based.
- **openpilot explicitly supports macOS natively** — this validates nelsonjchen's
  dockerless approach and suggests the native build is a supported path, not a hack.
- **The replay binary is C++ with vendored ncurses** — needs cereal/Cap'n Proto,
  FFmpeg libs, and OpenGL/EGL. NOT Qt-based (corrected from earlier assumption
  based on search results). This simplifies native deps considerably.
- **`--no-hw-decoder` flag exists** — provides a software decode fallback, which
  is useful for testing and for systems where HW decode is tricky to set up.
- **`--benchmark` mode** — can validate replay performance without needing the full
  UI capture pipeline. Good for Phase 0 smoke testing.
- **The fork's Python refactor is an advantage** — the upstream is shell-heavy (~33%),
  while the fork restructured into clean Python modules. This makes the native
  migration cleaner since less shell means fewer Docker-specific path assumptions.
- **The `--data_dir` flag on replay** is relevant for local SSH downloads — it lets
  you point replay at locally-stored route data instead of fetching from the cloud.
- **nelsonjchen credits @deanlee** for the replay tool — the level of effort is in
  openpilot's replay binary, not in the clipper wrapper. The clipper is fundamentally
  an orchestrator: download data → run replay + UI → capture screen → encode with ffmpeg.
