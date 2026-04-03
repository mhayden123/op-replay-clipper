# OP Replay Clipper (Native)

Generate openpilot replay clips locally — no Docker required.

This is a native port of [op-replay-clipper](https://github.com/mhayden123/op-replay-clipper) that runs the full rendering pipeline directly on your Linux machine with GPU acceleration.

## Prerequisites

- **Linux** — Ubuntu 22.04+, Linux Mint 21+, or Pop!_OS 22.04+ (apt-based)
- **NVIDIA GPU** with proprietary drivers installed (`nvidia-smi` must work)
- **15 GB free disk space** (openpilot clone + build artifacts)
- **git** installed

The install script handles everything else (Python, uv, openpilot, system packages).

## Install

```bash
git clone https://github.com/mhayden123/op-replay-clipper-native.git
cd op-replay-clipper-native
./install.sh
```

The installer takes 10-20 minutes on first run. It will:
1. Install system packages (build tools, ffmpeg, X11/EGL dev libraries)
2. Install [uv](https://github.com/astral-sh/uv) (fast Python package manager)
3. Clone openpilot and build the 5 native libraries the clipper needs
4. Build a patched pyray with headless EGL rendering support
5. Generate font atlases for the UI renderer

The script is **idempotent** — re-running it skips steps that are already complete.

## Usage

### Web UI

```bash
./start.sh
```

Opens `http://localhost:7860` in your browser. Paste a Comma Connect URL, pick a render type, and hit Clip.

### CLI

```bash
# Video transcode (no openpilot UI)
uv run python clip.py forward --demo

# Full UI render with openpilot overlay
uv run python clip.py ui --demo

# Render a specific route
uv run python clip.py ui "https://connect.comma.ai/<dongle>/<start>/<end>"

# SSH download from comma device on LAN
uv run python clip.py ui "<route>" --download-source ssh --device-ip 192.168.1.x
```

### Render types

| Type | Description |
|------|-------------|
| `ui` | openpilot UI overlay (default) |
| `ui-alt` | Alternative UI layout |
| `driver-debug` | Driver camera with debug info |
| `forward` | Forward camera transcode |
| `wide` | Wide camera transcode |
| `driver` | Driver camera transcode |
| `360` | Spherical 360 video |
| `forward_upon_wide` | Forward camera overlaid on wide |
| `360_forward_upon_wide` | 8K 360 with forward overlay |

## Management

```bash
./install.sh              # Re-run install (skips completed steps)
./install.sh --help       # Show all options and env vars
./install.sh --uninstall  # Remove ~/.op-replay-clipper/ (with confirmation)
```

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CLIPPER_HOME` | `~/.op-replay-clipper` | Base directory for all data |
| `OPENPILOT_ROOT` | `$CLIPPER_HOME/openpilot` | openpilot checkout location |
| `SCONS_JOBS` | `$(nproc)` | Parallel build jobs |
| `PORT` | `7860` | Web UI port (for start.sh) |

## How it works

The original op-replay-clipper runs in Docker containers with NVIDIA GPU passthrough. This version removes Docker entirely:

- `install.sh` sets up the same environment that `bootstrap_image_env.sh` creates inside Docker, but on your host system
- `web/server.py` invokes `clip.py` as a subprocess instead of spawning Docker containers
- All API endpoints are preserved — the [desktop app](https://github.com/mhayden123/op-replay-clipper-desktop) can talk to this server without changes

## Troubleshooting

**`nvidia-smi` not working?**
Install NVIDIA proprietary drivers via your distro's driver manager. The open-source `nouveau` driver won't work.

**Build fails during scons step?**
Make sure CUDA toolkit is installed: `nvcc --version`. If not, install it from NVIDIA's website for your Ubuntu version.

**UI renders show garbled text?**
Font atlases may be missing. Run: `./install.sh` (it will regenerate them if needed).

**Want to update openpilot?**
Delete the checkout and re-run install:
```bash
rm -rf ~/.op-replay-clipper/openpilot
./install.sh
```

## License

See [LICENSE.md](LICENSE.md) in the original repo.
