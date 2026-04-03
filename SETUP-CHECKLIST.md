# OP Replay Clipper Native Migration — Setup Checklist

> **Purpose:** Quick-reference checklist for setting up the development environment.
> Companion to `CLAUDE.md` (the migration plan).

---

## Distro Install (Desktop)

- [ ] Install Linux Mint (or Pop!_OS) on desktop — fresh, clean, apt-based
- [ ] Run full system update after install
- [ ] Verify internet and basic functionality

---

## NVIDIA GPU Setup

- [ ] Install NVIDIA proprietary drivers (Mint: Driver Manager, Pop: included with NVIDIA ISO)
- [ ] Verify with `nvidia-smi` — should show RTX 5080 and driver version
- [ ] Install CUDA Toolkit (12.x) — needed for openpilot native builds
  - Ubuntu/Mint: follow NVIDIA's official CUDA install guide for your distro version
  - Verify with `nvcc --version`
- [ ] Note: do NOT install NVIDIA Container Toolkit — the whole point is no Docker

---

## Core System Packages

These are the EXACT packages from `bootstrap_image_env.sh`. Install them natively:

```bash
sudo apt-get install -y \
  build-essential cmake jq ffmpeg faketime eatmydata htop mesa-utils bc \
  net-tools sudo wget curl capnproto git-lfs tzdata zstd git \
  libxrandr-dev libxinerama-dev libxcursor-dev libxi-dev libxext-dev \
  libegl1-mesa-dev xorg-dev xserver-xorg-core
```

- [ ] Run the above command
- [ ] **DO NOT** install `xserver-xorg-video-nvidia-525` — that's Docker-specific
      and will conflict with your host NVIDIA driver

openpilot's own `setup_dependencies.sh` adds a few more:
```bash
sudo apt-get install -y --no-install-recommends \
  ca-certificates build-essential curl libcurl4-openssl-dev locales git xvfb
```

- [ ] Run the above (some overlap with the first list, apt handles duplicates)

---

## Python Environment

- [ ] Python 3.12 — install via system package or deadsnakes PPA
  - `sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt install python3.12 python3.12-venv python3.12-dev`
  - Or use Mint's bundled Python if it's already 3.12+
- [ ] `uv` — Rust-based Python package manager
  - `curl -LsSf https://astral.sh/uv/install.sh | sh`
  - Verify with `uv --version`

---

## Claude Code

- [ ] Install Node.js (required for Claude Code CLI)
  - Recommended: use `nvm` or install from NodeSource
  - `curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash - && sudo apt install nodejs`
- [ ] Install Claude Code CLI
  - `npm install -g @anthropic-ai/claude-code`
- [ ] Verify with `claude --version`
- [ ] Log in / authenticate

---

## VS Code

- [ ] Install VS Code (`.deb` from Microsoft, or via Mint's Software Manager)

### Extensions — Must Have
- [ ] **Claude Code** (Anthropic) — Claude Code integration in VS Code
- [ ] **Python** (Microsoft) — Python language support
- [ ] **Pylance** (Microsoft) — Python type checking and IntelliSense
- [ ] **Ruff** (Astral) — fast Python linter/formatter (pairs with uv)
- [ ] **ShellCheck** (timonwong) — bash/shell script linting, critical for the install script work
- [ ] **Remote - SSH** (Microsoft) — if you want to code from the laptop targeting the desktop remotely
- [ ] **GitLens** (GitKraken) — enhanced Git history, blame, comparisons — useful when referencing the original repo

### Extensions — Nice to Have
- [ ] **YAML** (Red Hat) — syntax for docker-compose references and CI configs
- [ ] **Markdown Preview Enhanced** — better preview for the plan docs
- [ ] **Error Lens** — inline error/warning display, helps catch issues faster
- [ ] **Todo Tree** — surfaces TODO/FIXME/HACK comments across the project

---

## Claude Code Skills & Plugins

### Already Installed (keep these)
- [ ] `superpowers` (v5+) — planning with `writing-plans`, extended capabilities
- [ ] `frontend-design` — if you touch the web UI layer
- [ ] `feature-dev` — phase-based feature development (maps well to migration phases)
- [ ] `ui-overview` — UI structure awareness

### Consider Adding
- [ ] Check the marketplace for any `bash` / `shell-scripting` / `devops` skills
  that could help with the install script work in Phase 2
- [ ] Any `git` skills that help with repo setup, branching strategies

### Project Config
- [ ] Rename the migration plan to `CLAUDE.md` and place it in the repo root
  — Claude Code reads this automatically at session start
- [ ] As open questions get answered, update `CLAUDE.md` so every session starts informed

---

## Git & GitHub

- [ ] Configure git identity on the new machine
  - `git config --global user.name "..."`
  - `git config --global user.email "..."`
- [ ] Set up SSH key for GitHub
  - `ssh-keygen -t ed25519` → add public key to GitHub
- [ ] Create the new repo on GitHub (empty, no template)
- [ ] Clone locally and add `CLAUDE.md` as the first commit
- [ ] Set up GitHub CLI (`gh`) if you want it for PRs/issues from terminal
  - `sudo apt install gh && gh auth login`

---

## Validation Checklist (Run After Setup)

Once everything is installed, verify the full chain:

```bash
# GPU
nvidia-smi                    # Should show RTX 5080
nvcc --version                # Should show CUDA 12.x

# Python
python3.12 --version          # Should show 3.12.x
uv --version                  # Should show current version

# Build tools
gcc --version                 # Should show GCC
cmake --version               # Should show cmake 3.x+
ffmpeg -version               # Should show ffmpeg with NVIDIA codec support
git lfs version               # Should show git-lfs

# Claude Code
claude --version              # Should show current version

# Network
ssh -T git@github.com         # Should authenticate successfully
```

---

## Optional but Useful

- [ ] `htop` / `btop` — system monitoring during renders
- [ ] `nvtop` — GPU monitoring (shows CUDA utilization during renders)
- [ ] `tmux` or `screen` — terminal multiplexing for long render sessions
- [ ] `jq` — JSON processing from the command line (handy for API responses)
- [ ] `tree` — directory visualization
- [ ] `Timeshift` — system snapshots/backups (Mint includes this, good to enable
  before you start messing with system-level deps)
