# GlideKit

Clip. Render. Share your drives.

Desktop app for rendering [openpilot](https://github.com/commaai/openpilot) drive footage into shareable video clips. Paste a Comma Connect URL, pick a style, get an MP4.

Runs locally on your machine with GPU acceleration. No cloud, no Docker, no upload wait.

```bash
# Try it right now with a public demo route
git clone https://github.com/mhayden123/glidekit.git
cd glidekit
./install.sh    # one-time setup (~15 min)
./start.sh      # opens http://localhost:7860
```

## What You Get

**UI Overlay** — the full openpilot display with path prediction, lane lines, speed, and engagement state rendered over the road camera.

**Driver Debug** — driver monitoring camera with attention tracking, eye position, and head pose visualized in real time.

**Raw Camera** — forward, wide, or driver camera video transcoded to a clean MP4. No overlay, just the footage. Fastest render.

**360 Video** — spherical video from wide + driver cameras. Viewable in VLC, YouTube, or any 360 player.

All render types support file size targeting, HEVC output, and SSH download directly from your comma device.

## Install

### Linux

```bash
./install.sh
```

Installs everything: system packages, openpilot, GPU dependencies. Idempotent — safe to re-run. NVIDIA GPU recommended for NVENC encoding (CPU fallback works, just slower).

### Windows

Download the desktop app from [GlideKit Desktop Releases](https://github.com/mhayden123/glidekit-desktop/releases). It installs Python, Git, FFmpeg, and the clipper automatically. Double-click and go.

For camera-only renders (forward, wide, driver, 360) everything works natively. For UI renders, the app walks you through a one-time WSL setup.

### macOS (beta)

Same as Linux. Requires Homebrew. VideoToolbox hardware acceleration. Headless UI rendering is experimental.

## Usage

### Web UI

```bash
./start.sh
```

Paste a Comma Connect URL, pick a render type, hit Clip. Output opens in your video player when done.

### CLI

```bash
uv run python clip.py forward --demo                    # quick test
uv run python clip.py ui "https://connect.comma.ai/..."  # full UI render
uv run python clip.py forward "<route>" \
  --download-source ssh --device-ip 192.168.1.x          # grab from device on LAN
uv run python clip.py ui "<route>" -m 0 --file-format hevc  # max quality HEVC
```

### Desktop App

[GlideKit Desktop](https://github.com/mhayden123/glidekit-desktop/releases) wraps the web UI in a native window. Manages the server for you — no terminal.

## Render Types

| Type | What it renders | Needs openpilot |
|------|----------------|-----------------|
| `ui` | Full openpilot UI overlay | Yes |
| `ui-alt` | UI variant with steering wheel and confidence rail | Yes |
| `driver-debug` | Driver camera with DM state and pose telemetry | Yes |
| `forward` | Forward road camera | No |
| `wide` | Wide angle camera | No |
| `driver` | Driver-facing camera | No |
| `forward_upon_wide` | Forward projected onto wide via calibration | Yes |
| `360` | Spherical 360 from wide + driver | No |
| `360_forward_upon_wide` | 8K 360 with forward overlay | Yes |

## Platform Support

| | Linux | Windows | macOS |
|--|-------|---------|-------|
| Camera renders | Native | Native | Native |
| UI renders | Native | Via WSL | Beta |
| GPU encoding | NVIDIA NVENC | NVIDIA NVENC | VideoToolbox |
| CPU fallback | Yes | Yes | Yes |

## Requirements

- **Linux**: Ubuntu 22.04+, Mint, or Pop!_OS. NVIDIA GPU + drivers recommended.
- **Windows**: 10/11. Desktop app handles all dependencies.
- **macOS**: Homebrew + Xcode CLI tools.
- **Disk**: ~15 GB for openpilot + build artifacts.

## Credits

Built on [nelsonjchen's](https://github.com/nelsonjchen) op-replay-clipper. Uses [openpilot](https://github.com/commaai/openpilot) by [comma.ai](https://github.com/commaai), replay tooling by [deanlee](https://github.com/deanlee), and headless rendering patches by [ntegan1](https://github.com/ntegan1).

## License

[LICENSE.md](https://github.com/mhayden123/glidekit/blob/main/LICENSE.md)
