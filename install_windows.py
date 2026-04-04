"""GlideKit — Windows Installer

Sets up GlideKit for non-UI render types (forward, wide, driver, 360, etc.)
on Windows. UI render types (ui, ui-alt, driver-debug) require WSL with the
full Linux install — this script detects WSL and reports its availability.

No openpilot clone, no scons, no pyray — those aren't needed for non-UI renders.

Usage:
    python install_windows.py
    python install_windows.py --uninstall
    python install_windows.py --help
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from io import BytesIO
from pathlib import Path
from urllib.request import urlopen


CLIPPER_HOME = Path(os.environ.get("CLIPPER_HOME", Path.home() / ".glidekit"))
FFMPEG_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"


def log_step(msg: str) -> None:
    print(f"\n==> {msg}")


def log_ok(msg: str) -> None:
    print(f"  OK: {msg}")


def log_warn(msg: str) -> None:
    print(f"  WARN: {msg}")


def log_err(msg: str) -> None:
    print(f"  ERROR: {msg}")


def check_python() -> None:
    """Verify Python 3.12+."""
    log_step("Checking Python")
    v = sys.version_info
    if v.major < 3 or (v.major == 3 and v.minor < 12):
        log_err(f"Python {v.major}.{v.minor} found, but 3.12+ is required.")
        log_err("Download from https://www.python.org/downloads/")
        sys.exit(1)
    log_ok(f"Python {v.major}.{v.minor}.{v.micro}")


def install_uv() -> None:
    """Install uv if not present."""
    log_step("Checking uv")
    if shutil.which("uv"):
        result = subprocess.run(["uv", "--version"], capture_output=True, text=True)
        log_ok(f"uv already installed: {result.stdout.strip()}")
        return

    log_step("Installing uv")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--user", "uv"],
        check=True,
    )
    log_ok("uv installed via pip")


def install_ffmpeg() -> None:
    """Download static FFmpeg build if not on PATH."""
    log_step("Checking FFmpeg")
    if shutil.which("ffmpeg"):
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
        first_line = result.stdout.split("\n")[0] if result.stdout else "unknown"
        log_ok(f"FFmpeg already available: {first_line}")
        return

    ffmpeg_dir = CLIPPER_HOME / "ffmpeg"
    ffmpeg_exe = ffmpeg_dir / "ffmpeg.exe"
    if ffmpeg_exe.exists():
        log_ok(f"FFmpeg already downloaded: {ffmpeg_exe}")
        return

    log_step("Downloading static FFmpeg build")
    print(f"  From: {FFMPEG_URL}")
    print("  This may take a minute...")

    data = urlopen(FFMPEG_URL).read()
    with zipfile.ZipFile(BytesIO(data)) as zf:
        ffmpeg_dir.mkdir(parents=True, exist_ok=True)
        # Find the bin/ directory inside the zip
        for member in zf.namelist():
            if member.endswith("/bin/ffmpeg.exe"):
                bin_prefix = member.rsplit("ffmpeg.exe", 1)[0]
                for name in ("ffmpeg.exe", "ffprobe.exe"):
                    src = bin_prefix + name
                    if src in zf.namelist():
                        with zf.open(src) as f_in, open(ffmpeg_dir / name, "wb") as f_out:
                            f_out.write(f_in.read())
                break

    if ffmpeg_exe.exists():
        log_ok(f"FFmpeg downloaded to {ffmpeg_dir}")
        log_warn(f"Add {ffmpeg_dir} to your PATH for FFmpeg to be found automatically.")
    else:
        log_err("Failed to extract FFmpeg from zip")


def setup_directories() -> None:
    """Create the clipper directory structure."""
    log_step("Setting up directories")
    for d in ("output", "data"):
        (CLIPPER_HOME / d).mkdir(parents=True, exist_ok=True)

    config = CLIPPER_HOME / "config.env"
    config.write_text(
        f"# GlideKit — Windows Configuration\n"
        f"CLIPPER_HOME={CLIPPER_HOME}\n"
        f"CLIPPER_OUTPUT_DIR={CLIPPER_HOME / 'output'}\n"
        f"CLIPPER_DATA_DIR={CLIPPER_HOME / 'data'}\n"
    )
    log_ok(f"Directories: {CLIPPER_HOME}")


def check_gpu() -> None:
    """Check for NVIDIA GPU on Windows."""
    log_step("Checking GPU")
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            log_ok(f"NVIDIA GPU: {result.stdout.strip()}")
            return
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    log_warn("No NVIDIA GPU detected. Rendering will use CPU (slower).")


def check_wsl() -> None:
    """Check for WSL availability (needed for UI render types)."""
    log_step("Checking WSL (needed for UI render types)")
    try:
        result = subprocess.run(
            ["wsl.exe", "--list", "--verbose"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and "Running" in result.stdout:
            # Extract distro names
            lines = result.stdout.strip().split("\n")
            distros = [
                line.strip().split()[0].replace("*", "").strip()
                for line in lines[1:]
                if line.strip() and "Running" in line
            ]
            log_ok(f"WSL available: {', '.join(distros)}")
            log_ok("UI render types (ui, ui-alt, driver-debug) available via WSL")
            return
        elif result.returncode == 0:
            log_warn("WSL installed but no running distributions found.")
            log_warn("Start a WSL distro and run install.sh inside it for UI render support.")
            return
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    log_warn("WSL not detected. UI render types (ui, ui-alt, driver-debug) unavailable.")
    log_warn("Install WSL: wsl --install")
    log_warn("Non-UI render types (forward, wide, driver, 360, etc.) work without WSL.")


def install_clipper_deps() -> None:
    """Run uv sync for the clipper's Python dependencies."""
    log_step("Installing clipper Python dependencies")
    project_root = Path(__file__).resolve().parent
    if not (project_root / "pyproject.toml").exists():
        log_err(f"pyproject.toml not found in {project_root}")
        return

    subprocess.run(["uv", "sync"], cwd=str(project_root), check=True)
    log_ok("Python dependencies installed")


def do_uninstall() -> None:
    """Remove the clipper data directory."""
    if not CLIPPER_HOME.exists():
        print(f"Nothing to uninstall — {CLIPPER_HOME} does not exist.")
        return

    size = sum(f.stat().st_size for f in CLIPPER_HOME.rglob("*") if f.is_file()) / (1024 * 1024)
    print(f"This will permanently delete:")
    print(f"  {CLIPPER_HOME}")
    print(f"  Total size: {size:.0f} MB")
    print()
    confirm = input("Type 'yes' to confirm: ")
    if confirm != "yes":
        print("Cancelled.")
        return

    shutil.rmtree(CLIPPER_HOME)
    print(f"Removed {CLIPPER_HOME}")


def main() -> None:
    parser = argparse.ArgumentParser(description="GlideKit — Windows Installer")
    parser.add_argument("--uninstall", action="store_true", help="Remove clipper data")
    args = parser.parse_args()

    if args.uninstall:
        do_uninstall()
        return

    print("GlideKit — Windows Install")
    print("====================================")
    print(f"Target: {CLIPPER_HOME}")

    check_python()
    install_uv()
    install_ffmpeg()
    setup_directories()
    check_gpu()
    check_wsl()
    install_clipper_deps()

    print()
    print("=" * 44)
    print("  Installation complete!")
    print("=" * 44)
    print()
    print("Quick start:")
    print("  python start_server.py          # Launch web UI at http://localhost:7860")
    print("  uv run python clip.py forward --demo   # Smoke test")
    print()
    print("Available render types:")
    print("  forward, wide, driver, 360, forward_upon_wide, 360_forward_upon_wide")
    print()
    print("For UI render types (ui, ui-alt, driver-debug), set up WSL:")
    print("  wsl --install")
    print("  # Inside WSL: clone this repo and run ./install.sh")
    print()


if __name__ == "__main__":
    main()
