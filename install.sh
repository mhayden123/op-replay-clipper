#!/usr/bin/env bash

# OP Replay Clipper — Native Install Script
#
# Sets up the complete rendering environment on a host Linux system
# (Ubuntu/Mint/Pop!_OS) without Docker. Idempotent — safe to re-run;
# already-completed steps are detected and skipped automatically.

set -euo pipefail

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

ACTION="install"

show_help() {
  cat <<'USAGE'
Usage: ./install.sh [OPTIONS]

Install the OP Replay Clipper rendering environment natively (no Docker).

Options:
  -h, --help        Show this help message and exit
  --uninstall       Remove everything under ~/.op-replay-clipper/ (with confirmation)

Environment variables (all optional):
  CLIPPER_HOME       Base directory for all clipper data     (default: ~/.op-replay-clipper)
  OPENPILOT_ROOT     Where to clone openpilot                (default: $CLIPPER_HOME/openpilot)
  OPENPILOT_REPO_URL openpilot git URL                       (default: commaai/openpilot)
  OPENPILOT_BRANCH   openpilot branch to clone               (default: master)
  SCONS_JOBS         Parallel build jobs                     (default: $(nproc))
  SKIP_APT=1         Skip system package installation
  SKIP_OPENPILOT=1   Skip openpilot clone + deps + scons build
  SKIP_PYRAY=1       Skip patched pyray build
  SKIP_FONTS=1       Skip font atlas generation

The script is idempotent — re-running it automatically skips steps that are
already complete (openpilot cloned, scons built, pyray verified, fonts present).
SKIP_* flags force a step to be skipped even if detection would run it.

Examples:
  ./install.sh                     # Full install (skips completed steps)
  SKIP_APT=1 ./install.sh          # Skip apt (e.g. packages already installed)
  ./install.sh --uninstall          # Remove clipper data
USAGE
}

do_uninstall() {
  local home="${CLIPPER_HOME:-${HOME}/.op-replay-clipper}"
  if [[ ! -d "${home}" ]]; then
    echo "Nothing to uninstall — ${home} does not exist."
    exit 0
  fi

  echo "This will permanently delete:"
  echo "  ${home}"
  echo ""
  echo "This includes the openpilot checkout, built dependencies, rendered clips,"
  echo "and downloaded route data. The clipper source code (this repo) is NOT affected."
  echo ""
  du -sh "${home}" 2>/dev/null | awk '{print "  Total size: " $1}'
  echo ""
  read -r -p "Type 'yes' to confirm: " confirm
  if [[ "${confirm}" != "yes" ]]; then
    echo "Cancelled."
    exit 0
  fi

  rm -rf "${home}"
  echo "Removed ${home}"
  echo "To reinstall, run: ./install.sh"
}

for arg in "$@"; do
  case "${arg}" in
    -h|--help)   show_help; exit 0 ;;
    --uninstall) ACTION="uninstall" ;;
    *)           echo "Unknown option: ${arg}"; show_help; exit 1 ;;
  esac
done

if [[ "${ACTION}" == "uninstall" ]]; then
  do_uninstall
  exit 0
fi

# ---------------------------------------------------------------------------
# Section 1: Configuration & defaults
# ---------------------------------------------------------------------------

CLIPPER_HOME="${CLIPPER_HOME:-${HOME}/.op-replay-clipper}"
OPENPILOT_ROOT="${OPENPILOT_ROOT:-${CLIPPER_HOME}/openpilot}"
OPENPILOT_REPO_URL="${OPENPILOT_REPO_URL:-https://github.com/commaai/openpilot.git}"
OPENPILOT_BRANCH="${OPENPILOT_BRANCH:-master}"
OPENPILOT_CLONE_DEPTH="${OPENPILOT_CLONE_DEPTH:-1}"
SCONS_JOBS="${SCONS_JOBS:-$(nproc 2>/dev/null || echo 8)}"
export DEBIAN_FRONTEND=noninteractive

# Packages needed for the rendering pipeline.
# Sourced from the original bootstrap_image_env.sh APT_PACKAGES list.
#
# GOTCHA #6: xserver-xorg-video-nvidia-525 is EXCLUDED.
# That package provides the X11 NVIDIA driver inside Docker containers.
# On a native install the host NVIDIA driver already provides this.
# Installing it can CONFLICT with your host driver — do not add it back.
APT_PACKAGES=(
  build-essential
  cmake
  jq
  ffmpeg
  eatmydata
  htop
  mesa-utils
  bc
  net-tools
  wget
  curl
  capnproto
  git-lfs
  tzdata
  zstd
  git
  xserver-xorg-core
  libxrandr-dev
  libxinerama-dev
  libxcursor-dev
  libxi-dev
  libxext-dev
  libegl1-mesa-dev
  xorg-dev
  # Additional packages from openpilot's setup_dependencies.sh
  ca-certificates
  libcurl4-openssl-dev
  locales
  xvfb
)

# Packages deliberately excluded from the Docker list:
#   - xserver-xorg-video-nvidia-525  (Docker-specific, see GOTCHA #6)
#   - faketime                        (likely unused by clipper, verify later)
#   - sudo                            (already on host)

# ---------------------------------------------------------------------------
# Section 2: Logging & utility helpers
# ---------------------------------------------------------------------------

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_step() {
  printf '\n%b==> %s%b\n' "${BLUE}" "$1" "${NC}"
}

log_ok() {
  printf '%b  OK: %s%b\n' "${GREEN}" "$1" "${NC}"
}

log_warn() {
  printf '%b  WARN: %s%b\n' "${YELLOW}" "$1" "${NC}"
}

log_err() {
  printf '%b  ERROR: %s%b\n' "${RED}" "$1" "${NC}"
}

die() {
  log_err "$1"
  exit 1
}

# ---------------------------------------------------------------------------
# Section 3: Prerequisite validation
# ---------------------------------------------------------------------------

preflight_checks() {
  log_step "Running preflight checks"

  # Must be Linux
  [[ "$(uname -s)" == "Linux" ]] || die "This script only supports Linux."

  # Must be apt-based (Phase 0 targets Ubuntu/Mint/Pop only)
  command -v apt-get >/dev/null 2>&1 || die "apt-get not found. This script requires an apt-based distro (Ubuntu, Mint, Pop!_OS)."

  # NVIDIA driver check — warn but don't block (GPU only needed at render time)
  if ! command -v nvidia-smi >/dev/null 2>&1; then
    log_warn "nvidia-smi not found. NVIDIA drivers are required for GPU-accelerated rendering."
  elif ! nvidia-smi >/dev/null 2>&1; then
    log_warn "nvidia-smi failed (driver/library mismatch or no GPU). Rendering will need a working GPU later."
  else
    log_ok "NVIDIA driver: $(nvidia-smi --query-gpu=driver_version --format=csv,noheader,nounits | head -1)"
    log_ok "GPU: $(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)"
  fi

  # Check for CUDA toolkit (nvcc). Warn if missing but don't block —
  # openpilot's setup_dependencies.sh may handle this, and the scons build
  # doesn't compile CUDA kernels (GOTCHA: scons builds Cython .so files, not CUDA).
  if command -v nvcc >/dev/null 2>&1; then
    log_ok "CUDA toolkit: $(nvcc --version | grep 'release' | sed 's/.*release //' | sed 's/,.*//')"
  else
    log_warn "nvcc not found. CUDA toolkit may be needed — openpilot's setup may install it."
  fi

  # Python 3.12+
  if command -v python3.12 >/dev/null 2>&1; then
    log_ok "Python 3.12: $(python3.12 --version)"
  elif command -v python3 >/dev/null 2>&1; then
    local py_version
    py_version="$(python3 --version | grep -oP '\d+\.\d+')"
    if [[ "$(echo "${py_version} >= 3.12" | bc -l)" == "1" ]]; then
      log_ok "Python: $(python3 --version)"
    else
      log_warn "Python ${py_version} found, but 3.12+ is recommended. openpilot's setup may handle this."
    fi
  else
    log_warn "Python 3 not found. Will be installed via apt packages."
  fi

  # git
  command -v git >/dev/null 2>&1 || die "git not found. Install git first."
  log_ok "git: $(git --version)"

  # Disk space — openpilot clone + build needs ~10-15 GB
  local available_gb
  available_gb=$(df --output=avail "${CLIPPER_HOME%/*}" 2>/dev/null | tail -1 | awk '{printf "%.0f", $1/1048576}')
  if [[ -n "${available_gb}" ]] && (( available_gb < 15 )); then
    log_warn "Only ${available_gb} GB free at ${CLIPPER_HOME%/*}. Recommend at least 15 GB."
  else
    log_ok "Disk space: ${available_gb} GB available"
  fi

  log_ok "All preflight checks passed"
}

# ---------------------------------------------------------------------------
# Section 4: System package installation
# ---------------------------------------------------------------------------

install_system_packages() {
  if [[ "${SKIP_APT:-0}" == "1" ]]; then
    log_step "Skipping system packages (SKIP_APT=1)"
    return
  fi

  log_step "Installing system packages"
  sudo apt-get update -y
  sudo apt-get install -y "${APT_PACKAGES[@]}"

  # Configure git-lfs (needs to run once after install)
  git lfs install
  log_ok "System packages installed"
}

# ---------------------------------------------------------------------------
# Section 5: uv (Python package manager) installation
# ---------------------------------------------------------------------------

ensure_uv() {
  log_step "Ensuring uv is available"

  if command -v uv >/dev/null 2>&1; then
    log_ok "uv already installed: $(uv --version)"
    return
  fi

  # Check common install locations before downloading
  local uv_paths=(
    "${HOME}/.local/bin/uv"
    "${HOME}/.cargo/bin/uv"
    "/usr/local/bin/uv"
  )
  for p in "${uv_paths[@]}"; do
    if [[ -x "${p}" ]]; then
      export PATH="$(dirname "${p}"):${PATH}"
      log_ok "Found uv at ${p}"
      return
    fi
  done

  # Install uv
  curl -LsSf https://astral.sh/uv/install.sh | sh

  # Add to PATH for this session
  export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH}"

  command -v uv >/dev/null 2>&1 || die "uv installation failed"
  log_ok "uv installed: $(uv --version)"
}

# ---------------------------------------------------------------------------
# Section 6: Clone openpilot
# ---------------------------------------------------------------------------

clone_openpilot() {
  if [[ "${SKIP_OPENPILOT:-0}" == "1" ]]; then
    log_step "Skipping openpilot clone (SKIP_OPENPILOT=1)"
    return
  fi

  log_step "Cloning openpilot into ${OPENPILOT_ROOT}"

  mkdir -p "$(dirname "${OPENPILOT_ROOT}")"

  if [[ -d "${OPENPILOT_ROOT}/.git" ]]; then
    log_warn "openpilot directory already exists at ${OPENPILOT_ROOT}"
    echo "  To re-clone, remove it first: rm -rf ${OPENPILOT_ROOT}"
    echo "  Continuing with existing checkout..."
    return
  fi

  # Shallow partial clone — minimizes bandwidth and disk.
  # Uses --filter=blob:none (treeless clone): git objects fetched on-demand.
  # GOTCHA #8: Network must be available during subsequent build steps
  # because the partial clone may need to fetch additional blobs.
  git clone \
    --branch "${OPENPILOT_BRANCH}" \
    --depth "${OPENPILOT_CLONE_DEPTH}" \
    --filter=blob:none \
    --recurse-submodules \
    --shallow-submodules \
    --single-branch \
    "${OPENPILOT_REPO_URL}" \
    "${OPENPILOT_ROOT}"

  log_ok "openpilot cloned (branch: ${OPENPILOT_BRANCH})"
}

# ---------------------------------------------------------------------------
# Section 7: Install openpilot's own dependencies
# ---------------------------------------------------------------------------
#
# GOTCHA #7: openpilot's setup_dependencies.sh runs `uv sync --all-extras`.
# This installs ALL optional Python deps (dev tools, testing, etc.).
# We keep --all-extras because changing it could break assumptions in the
# scons build or font generation. Optimize later once the pipeline is proven.
#
# GOTCHA #9: This creates openpilot's OWN Python venv at $OPENPILOT_ROOT/.venv.
# This is SEPARATE from the clipper's venv. The patched pyray, scons-built
# .so files, and fonts all live inside openpilot's venv. Do not mix them up.

install_openpilot_dependencies() {
  if [[ "${SKIP_OPENPILOT:-0}" == "1" ]]; then
    log_step "Skipping openpilot dependencies (SKIP_OPENPILOT=1)"
    return
  fi

  # Idempotent: if the venv already exists and has packages, skip
  if [[ -x "${OPENPILOT_ROOT}/.venv/bin/python" ]]; then
    local pkg_count
    pkg_count="$("${OPENPILOT_ROOT}/.venv/bin/python" -c "import importlib.metadata; print(len(list(importlib.metadata.distributions())))" 2>/dev/null || echo 0)"
    if (( pkg_count > 50 )); then
      log_step "openpilot dependencies already installed (${pkg_count} packages)"
      return
    fi
  fi

  log_step "Installing openpilot dependencies"
  cd "${OPENPILOT_ROOT}"

  if [[ -x ./tools/setup_dependencies.sh ]]; then
    ./tools/setup_dependencies.sh
  elif [[ -x ./tools/ubuntu_setup.sh ]]; then
    # Fallback for older openpilot versions
    INSTALL_EXTRA_PACKAGES=yes ./tools/ubuntu_setup.sh
    ./tools/install_python_dependencies.sh
  else
    die "No supported openpilot dependency setup script found in ${OPENPILOT_ROOT}/tools/"
  fi

  log_ok "openpilot dependencies installed"
}

# ---------------------------------------------------------------------------
# Section 8: Fix vendored tool permissions
# ---------------------------------------------------------------------------
#
# openpilot bundles some prebuilt binaries (capnp, ffmpeg, arm toolchain) inside
# its venv. They may not have execute permissions after uv sync.

fix_vendored_tool_permissions() {
  if [[ ! -d "${OPENPILOT_ROOT}/.venv/lib" ]]; then
    log_warn "openpilot venv not found, skipping vendored tool permission fix"
    return
  fi

  log_step "Fixing vendored tool permissions"
  find "${OPENPILOT_ROOT}/.venv/lib" -type f \
    \( -name 'arm-none-eabi-*' -o -name 'capnp' -o -name 'capnpc*' -o -name 'ffmpeg' -o -name 'ffprobe' \) \
    -exec chmod +x {} + 2>/dev/null || true

  log_ok "Vendored tool permissions fixed"
}

# ---------------------------------------------------------------------------
# Section 9: Build targeted openpilot native dependencies (scons)
# ---------------------------------------------------------------------------
#
# GOTCHA #3: This is a TARGETED build of exactly 5 Cython .so files.
# Do NOT run scons without specifying targets — that builds ALL of openpilot
# and takes forever. These 5 files are all the clipper needs:
#   - msgq IPC and visionipc (inter-process communication)
#   - params (openpilot parameter system)
#   - MPC solvers (longitudinal and lateral)

SCONS_TARGETS=(
  "msgq_repo/msgq/ipc_pyx.so"
  "msgq_repo/msgq/visionipc/visionipc_pyx.so"
  "common/params_pyx.so"
  "selfdrive/controls/lib/longitudinal_mpc_lib/c_generated_code/acados_ocp_solver_pyx.so"
  "selfdrive/controls/lib/lateral_mpc_lib/c_generated_code/acados_ocp_solver_pyx.so"
)

build_openpilot_clip_dependencies() {
  if [[ "${SKIP_OPENPILOT:-0}" == "1" ]]; then
    log_step "Skipping openpilot scons build (SKIP_OPENPILOT=1)"
    return
  fi

  # Idempotent: if all 5 .so files exist, skip the build
  local all_built=true
  for target in "${SCONS_TARGETS[@]}"; do
    if [[ ! -f "${OPENPILOT_ROOT}/${target}" ]]; then
      all_built=false
      break
    fi
  done
  if [[ "${all_built}" == "true" ]]; then
    log_step "scons targets already built (all 5 .so files present)"
    return
  fi

  log_step "Building native openpilot clip dependencies (scons, ${SCONS_JOBS} jobs)"
  cd "${OPENPILOT_ROOT}"

  uv run --no-sync scons -j"${SCONS_JOBS}" "${SCONS_TARGETS[@]}"

  log_ok "openpilot clip dependencies built"
}

# ---------------------------------------------------------------------------
# Section 10: Build patched pyray with EGL pbuffer support
# ---------------------------------------------------------------------------
#
# GOTCHA #2: This is THE hardest and most critical step. A normal
# `pip install pyray` will NOT work for headless rendering.
#
# What this does:
#   1. Clone comma's fork of raylib
#   2. Patch GLFW to add a null platform with EGL pbuffer surfaces,
#      triggered by OPENPILOT_UI_NULL_EGL=1 env var
#   3. Build raylib as a static library (libraylib.a)
#   4. Clone comma's fork of raylib-python-cffi
#   5. Patch its build.py to statically link libraylib.a and add -lEGL
#   6. Build a wheel and install it into OPENPILOT's venv (not the clipper's!)
#   7. Verify the installed pyray has the patches
#
# The standalone build_linux_pyray_null_egl.py from the source repo is used
# here via a faithful shell invocation. The Python script handles all the
# patching, cmake, and wheel building.

_pyray_is_patched() {
  # Returns 0 if the patched pyray is already correctly installed in openpilot's venv.
  local op_python="${OPENPILOT_ROOT}/.venv/bin/python"
  [[ -x "${op_python}" ]] || return 1
  "${op_python}" -c "
from pathlib import Path
import raylib
base = Path(raylib.__file__).resolve().parent
build = (base / 'build.py').read_text()
assert \"os.path.join(get_the_lib_path(), 'libraylib.a')\" in build
assert \"'-lEGL'\" in build
" 2>/dev/null
}

install_patched_pyray() {
  if [[ "${SKIP_PYRAY:-0}" == "1" ]]; then
    log_step "Skipping patched pyray build (SKIP_PYRAY=1)"
    return
  fi

  # Idempotent: if the patched pyray is already verified, skip
  if _pyray_is_patched; then
    log_step "Patched pyray already installed and verified"
    return
  fi

  local op_python="${OPENPILOT_ROOT}/.venv/bin/python"

  if [[ ! -x "${op_python}" ]]; then
    die "openpilot venv python not found at ${op_python}. Run openpilot dependency setup first."
  fi

  log_step "Building patched pyray (EGL null pbuffer support)"
  echo "  This clones comma's raylib + raylib-python-cffi forks,"
  echo "  patches GLFW for headless EGL rendering, and builds a wheel."
  echo "  This may take a few minutes..."

  # Use the standalone build script if it exists in the repo,
  # otherwise use the embedded version from bootstrap_image_env.sh.
  local build_script="${SCRIPT_DIR}/common/build_linux_pyray_null_egl.py"

  if [[ -f "${build_script}" ]]; then
    python3 "${build_script}" --python-bin "${op_python}"
  else
    # Inline the pyray build — this is a direct port of the embedded Python
    # heredoc from install_accelerated_linux_pyray() in bootstrap_image_env.sh.
    # It is identical to build_linux_pyray_null_egl.py but invoked inline.
    python3 - "${op_python}" <<'PYRAY_BUILD_SCRIPT'
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

python_bin = sys.argv[1]
raylib_repo = "https://github.com/commaai/raylib.git"
pyray_repo = "https://github.com/commaai/raylib-python-cffi.git"
raygui_url = (
    "https://raw.githubusercontent.com/raysan5/raygui/"
    "76b36b597edb70ffaf96f046076adc20d67e7827/src/raygui.h"
)


def run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    print(f"+ {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env, check=True)


def capture(cmd: list[str], *, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout.strip()


def ensure_pip(python_bin: str) -> None:
    try:
        run([python_bin, "-m", "pip", "--version"])
    except subprocess.CalledProcessError:
        run([python_bin, "-m", "ensurepip", "--upgrade"])
        run([python_bin, "-m", "pip", "install", "--upgrade", "pip", "wheel", "setuptools"])


def verify_installed_pyray(python_bin: str) -> None:
    check = (
        "from pathlib import Path\n"
        "import raylib\n"
        "base = Path(raylib.__file__).resolve().parent\n"
        "build = (base / 'build.py').read_text()\n"
        "version = (base / 'version.py').read_text().strip()\n"
        "print(version)\n"
        "assert \"os.path.join(get_the_lib_path(), 'libraylib.a')\" in build\n"
        "assert \"'-lEGL'\" in build\n"
    )
    run([python_bin, "-c", check])


def replace_once(text: str, needle: str, replacement: str, *, label: str) -> str:
    if replacement in text:
        return text
    if needle not in text:
        raise RuntimeError(f"Could not find {label} patch anchor")
    return text.replace(needle, replacement, 1)


def patch_checkout(raylib_dir: Path) -> None:
    internal = raylib_dir / "src/external/glfw/src/internal.h"
    text = internal.read_text()
    text = replace_once(
        text,
        "#define EGL_WINDOW_BIT 0x0004\n",
        "#define EGL_PBUFFER_BIT 0x0001\n#define EGL_WINDOW_BIT 0x0004\n",
        label="EGL_PBUFFER_BIT",
    )
    text = replace_once(
        text,
        "#define EGL_NATIVE_VISUAL_ID 0x302e\n",
        "#define EGL_NATIVE_VISUAL_ID 0x302e\n#define EGL_WIDTH 0x3057\n#define EGL_HEIGHT 0x3056\n",
        label="EGL pbuffer dimensions",
    )
    text = replace_once(
        text,
        "typedef EGLSurface (APIENTRY * PFN_eglCreateWindowSurface)(EGLDisplay,EGLConfig,EGLNativeWindowType,const EGLint*);\n",
        "typedef EGLSurface (APIENTRY * PFN_eglCreateWindowSurface)(EGLDisplay,EGLConfig,EGLNativeWindowType,const EGLint*);\n"
        "typedef EGLSurface (APIENTRY * PFN_eglCreatePbufferSurface)(EGLDisplay,EGLConfig,const EGLint*);\n",
        label="PFN_eglCreatePbufferSurface typedef",
    )
    text = replace_once(
        text,
        "#define eglCreateWindowSurface _glfw.egl.CreateWindowSurface\n",
        "#define eglCreateWindowSurface _glfw.egl.CreateWindowSurface\n"
        "#define eglCreatePbufferSurface _glfw.egl.CreatePbufferSurface\n",
        label="eglCreatePbufferSurface macro",
    )
    text = replace_once(
        text,
        "        PFN_eglCreateWindowSurface  CreateWindowSurface;\n",
        "        PFN_eglCreateWindowSurface  CreateWindowSurface;\n"
        "        PFN_eglCreatePbufferSurface CreatePbufferSurface;\n",
        label="CreatePbufferSurface field",
    )
    internal.write_text(text)

    platform_c = raylib_dir / "src/external/glfw/src/platform.c"
    text = platform_c.read_text()
    text = replace_once(
        text,
        "#if defined(_GLFW_X11)\n    { GLFW_PLATFORM_X11, _glfwConnectX11 },\n#endif\n};\n",
        "#if defined(_GLFW_X11)\n    { GLFW_PLATFORM_X11, _glfwConnectX11 },\n#endif\n"
        "    { GLFW_PLATFORM_NULL, _glfwConnectNull },\n};\n",
        label="null platform selector",
    )
    text = replace_once(
        text,
        "    const size_t count = sizeof(supportedPlatforms) / sizeof(supportedPlatforms[0]);\n    size_t i;\n\n",
        "    const size_t count = sizeof(supportedPlatforms) / sizeof(supportedPlatforms[0]);\n    size_t i;\n\n"
        "    if (getenv(\"OPENPILOT_UI_NULL_EGL\"))\n    {\n"
        "        fprintf(stderr, \"GLFW forced null connect\\n\");\n"
        "        return _glfwConnectNull(GLFW_PLATFORM_NULL, platform);\n"
        "    }\n\n",
        label="null platform env override",
    )
    platform_c.write_text(text)

    rcore = raylib_dir / "src/platforms/rcore_desktop_glfw.c"
    text = rcore.read_text()
    text = replace_once(
        text,
        "#if defined(__APPLE__)\n    glfwInitHint(GLFW_COCOA_CHDIR_RESOURCES, GLFW_FALSE);\n#endif\n    // Initialize GLFW internal global state\n",
        "#if defined(__APPLE__)\n    glfwInitHint(GLFW_COCOA_CHDIR_RESOURCES, GLFW_FALSE);\n#endif\n"
        "    if (getenv(\"OPENPILOT_UI_NULL_EGL\")) glfwInitHint(GLFW_PLATFORM, GLFW_PLATFORM_NULL);\n"
        "    // Initialize GLFW internal global state\n",
        label="glfwInit null hint",
    )
    text = replace_once(
        text,
        "    glfwDefaultWindowHints();                       // Set default windows hints\n",
        "    glfwDefaultWindowHints();                       // Set default windows hints\n"
        "    if (getenv(\"OPENPILOT_UI_NULL_EGL\")) glfwWindowHint(GLFW_CONTEXT_CREATION_API, GLFW_EGL_CONTEXT_API);\n",
        label="glfw EGL hint",
    )
    rcore.write_text(text)

    egl = raylib_dir / "src/external/glfw/src/egl_context.c"
    text = egl.read_text()
    text = replace_once(
        text,
        "    _glfw.egl.CreateWindowSurface = (PFN_eglCreateWindowSurface)\n        _glfwPlatformGetModuleSymbol(_glfw.egl.handle, \"eglCreateWindowSurface\");\n",
        "    _glfw.egl.CreateWindowSurface = (PFN_eglCreateWindowSurface)\n"
        "        _glfwPlatformGetModuleSymbol(_glfw.egl.handle, \"eglCreateWindowSurface\");\n"
        "    _glfw.egl.CreatePbufferSurface = (PFN_eglCreatePbufferSurface)\n"
        "        _glfwPlatformGetModuleSymbol(_glfw.egl.handle, \"eglCreatePbufferSurface\");\n",
        label="eglCreatePbufferSurface loader",
    )
    text = replace_once(
        text,
        "        !_glfw.egl.CreateWindowSurface ||\n",
        "        !_glfw.egl.CreateWindowSurface ||\n"
        "        !_glfw.egl.CreatePbufferSurface ||\n",
        label="CreatePbufferSurface required check",
    )
    text = replace_once(
        text,
        "        // Only consider window EGLConfigs\n        if (!(getEGLConfigAttrib(n, EGL_SURFACE_TYPE) & EGL_WINDOW_BIT))\n            continue;\n",
        "        // Only consider surface-capable configs\n"
        "        if (_glfw.platform.platformID == GLFW_PLATFORM_NULL)\n"
        "        {\n"
        "            if (!(getEGLConfigAttrib(n, EGL_SURFACE_TYPE) & EGL_PBUFFER_BIT))\n"
        "                continue;\n"
        "        }\n"
        "        else\n"
        "        {\n"
        "            if (!(getEGLConfigAttrib(n, EGL_SURFACE_TYPE) & EGL_WINDOW_BIT))\n"
        "                continue;\n"
        "        }\n",
        label="null EGL config filtering",
    )
    text = replace_once(
        text,
        "    native = _glfw.platform.getEGLNativeWindow(window);\n"
        "    // HACK: ANGLE does not implement eglCreatePlatformWindowSurfaceEXT\n"
        "    //       despite reporting EGL_EXT_platform_base\n"
        "    if (_glfw.egl.platform && _glfw.egl.platform != EGL_PLATFORM_ANGLE_ANGLE)\n"
        "    {\n"
        "        window->context.egl.surface =\n"
        "            eglCreatePlatformWindowSurfaceEXT(_glfw.egl.display, config, native, attribs);\n"
        "    }\n"
        "    else\n"
        "    {\n"
        "        window->context.egl.surface =\n"
        "            eglCreateWindowSurface(_glfw.egl.display, config, native, attribs);\n"
        "    }\n",
        "    if (_glfw.platform.platformID == GLFW_PLATFORM_NULL)\n"
        "    {\n"
        "        const EGLint pbufferAttribs[] = {\n"
        "            EGL_WIDTH, window->null.width > 0 ? window->null.width : 1,\n"
        "            EGL_HEIGHT, window->null.height > 0 ? window->null.height : 1,\n"
        "            EGL_NONE\n"
        "        };\n"
        "        window->context.egl.surface =\n"
        "            eglCreatePbufferSurface(_glfw.egl.display, config, pbufferAttribs);\n"
        "    }\n"
        "    else\n"
        "    {\n"
        "        native = _glfw.platform.getEGLNativeWindow(window);\n"
        "        // HACK: ANGLE does not implement eglCreatePlatformWindowSurfaceEXT\n"
        "        //       despite reporting EGL_EXT_platform_base\n"
        "        if (_glfw.egl.platform && _glfw.egl.platform != EGL_PLATFORM_ANGLE_ANGLE)\n"
        "        {\n"
        "            window->context.egl.surface =\n"
        "                eglCreatePlatformWindowSurfaceEXT(_glfw.egl.display, config, native, attribs);\n"
        "        }\n"
        "        else\n"
        "        {\n"
        "            window->context.egl.surface =\n"
        "                eglCreateWindowSurface(_glfw.egl.display, config, native, attribs);\n"
        "        }\n"
        "    }\n",
        label="null pbuffer surface creation",
    )
    egl.write_text(text)


def patch_pyray_checkout(pyray_dir: Path) -> None:
    build_py = pyray_dir / "raylib/build.py"
    text = build_py.read_text()
    text = replace_once(
        text,
        "        extra_link_args = get_lib_flags() + [ '-lm', '-lpthread', '-lGL',\n"
        "                                              '-lrt', '-lm', '-ldl', '-lpthread', '-latomic']\n",
        "        extra_link_args = [os.path.join(get_the_lib_path(), 'libraylib.a'), '-lm', '-lpthread', '-lGL',\n"
        "                           '-lEGL', '-lrt', '-lm', '-ldl', '-lpthread', '-latomic']\n",
        label="direct static raylib link",
    )
    build_py.write_text(text)


with tempfile.TemporaryDirectory(prefix="pyray-null-egl-") as tmp:
    tmpdir = Path(tmp)
    raylib_dir = tmpdir / "raylib"
    pyray_dir = tmpdir / "raylib-python-cffi"
    stage_dir = tmpdir / "stage"
    include_dir = stage_dir / "include"
    glfw_include_dir = include_dir / "GLFW"
    lib_dir = stage_dir / "lib"
    glfw_include_dir.mkdir(parents=True, exist_ok=True)
    lib_dir.mkdir(parents=True, exist_ok=True)

    run(["git", "clone", "--depth=1", raylib_repo, str(raylib_dir)])
    patch_checkout(raylib_dir)
    run(
        [
            "cmake",
            "-S",
            str(raylib_dir),
            "-B",
            str(raylib_dir / "build"),
            "-DPLATFORM=Desktop",
            "-DGLFW_BUILD_WAYLAND=OFF",
            "-DGLFW_BUILD_X11=ON",
            "-DBUILD_SHARED_LIBS=OFF",
            "-DCMAKE_BUILD_TYPE=Release",
            "-DWITH_PIC=ON",
            "-DBUILD_EXAMPLES=OFF",
            "-DBUILD_GAMES=OFF",
        ]
    )
    jobs = capture(["bash", "-lc", "nproc || echo 8"])
    run(["cmake", "--build", str(raylib_dir / "build"), "-j", jobs])

    shutil.copy2(raylib_dir / "build/raylib/libraylib.a", lib_dir / "libraylib.a")
    for header in ("raylib.h", "rlgl.h", "raymath.h"):
        shutil.copy2(raylib_dir / "src" / header, include_dir / header)
    shutil.copy2(raylib_dir / "src/external/glfw/include/GLFW/glfw3.h", glfw_include_dir / "glfw3.h")
    run(["curl", "-fsSLo", str(include_dir / "raygui.h"), raygui_url])

    run(["git", "clone", "--depth=1", pyray_repo, str(pyray_dir)])
    patch_pyray_checkout(pyray_dir)
    env = dict(os.environ)
    env["RAYLIB_PLATFORM"] = "Desktop"
    env["RAYLIB_INCLUDE_PATH"] = str(include_dir)
    env["RAYLIB_LIB_PATH"] = str(lib_dir)
    ensure_pip(python_bin)
    run([python_bin, "-m", "pip", "wheel", ".", "-w", "dist"], cwd=pyray_dir, env=env)
    wheels = sorted((pyray_dir / "dist").glob("*.whl"))
    if not wheels:
        raise RuntimeError("No pyray wheel was built")
    run([python_bin, "-m", "pip", "install", "--force-reinstall", *map(str, wheels)])
    verify_installed_pyray(python_bin)
PYRAY_BUILD_SCRIPT
  fi

  log_ok "Patched pyray installed into openpilot venv"
}

# ---------------------------------------------------------------------------
# Section 11: Generate UI font atlases
# ---------------------------------------------------------------------------
#
# GOTCHA #11: If this fails or is skipped, UI renders will show missing/garbled
# text. Fonts must be regenerated when openpilot is updated. Requires the
# openpilot venv to be fully set up (after uv sync + scons build).

generate_ui_fonts() {
  if [[ "${SKIP_FONTS:-0}" == "1" ]]; then
    log_step "Skipping font atlas generation (SKIP_FONTS=1)"
    return
  fi

  # Idempotent: if font .png files already exist, skip
  local font_count
  font_count="$(find "${OPENPILOT_ROOT}/selfdrive/assets/fonts" -name '*.png' 2>/dev/null | wc -l)"
  if (( font_count >= 10 )); then
    log_step "Font atlases already present (${font_count} files)"
    return
  fi

  log_step "Generating UI font atlases"
  cd "${OPENPILOT_ROOT}"
  uv run --no-sync python selfdrive/assets/fonts/process.py

  log_ok "Font atlases generated"
}

# ---------------------------------------------------------------------------
# Section 12: Record openpilot commit hash
# ---------------------------------------------------------------------------

record_openpilot_commit() {
  if [[ ! -d "${OPENPILOT_ROOT}/.git" ]]; then
    return
  fi

  log_step "Recording openpilot commit"
  cd "${OPENPILOT_ROOT}"
  git rev-parse HEAD > "${OPENPILOT_ROOT}/COMMIT"
  log_ok "Commit: $(cat "${OPENPILOT_ROOT}/COMMIT")"
}

# ---------------------------------------------------------------------------
# Section 13: Create directory structure & write config
# ---------------------------------------------------------------------------

setup_clipper_directories() {
  log_step "Setting up clipper directory structure"

  mkdir -p "${CLIPPER_HOME}/output"
  mkdir -p "${CLIPPER_HOME}/data"

  # Write a small config file that clip.py and server.py can reference
  cat > "${CLIPPER_HOME}/config.env" <<EOF
# OP Replay Clipper — Native Configuration
# Generated by install.sh on $(date -Iseconds)
CLIPPER_HOME=${CLIPPER_HOME}
OPENPILOT_ROOT=${OPENPILOT_ROOT}
CLIPPER_OUTPUT_DIR=${CLIPPER_HOME}/output
CLIPPER_DATA_DIR=${CLIPPER_HOME}/data
OPENPILOT_UI_NULL_EGL=1
EOF

  log_ok "Directories created:"
  echo "  Base:    ${CLIPPER_HOME}"
  echo "  Output:  ${CLIPPER_HOME}/output"
  echo "  Data:    ${CLIPPER_HOME}/data"
  echo "  Config:  ${CLIPPER_HOME}/config.env"
}

# ---------------------------------------------------------------------------
# Section 14: Final validation
# ---------------------------------------------------------------------------

validate_install() {
  log_step "Validating installation"
  local errors=0

  # Check openpilot venv exists (GOTCHA #10: clip.py checks for .venv/bin/python)
  if [[ -x "${OPENPILOT_ROOT}/.venv/bin/python" ]]; then
    log_ok "openpilot venv: ${OPENPILOT_ROOT}/.venv/bin/python"
  else
    log_err "openpilot venv python not found"
    ((errors++))
  fi

  # Check scons build artifacts
  local scons_targets=(
    "msgq_repo/msgq/ipc_pyx.so"
    "msgq_repo/msgq/visionipc/visionipc_pyx.so"
    "common/params_pyx.so"
  )
  for target in "${scons_targets[@]}"; do
    if [[ -f "${OPENPILOT_ROOT}/${target}" ]]; then
      log_ok "scons target: ${target}"
    else
      log_err "scons target missing: ${target}"
      ((errors++))
    fi
  done

  # Check patched pyray
  if "${OPENPILOT_ROOT}/.venv/bin/python" -c "import raylib; print(raylib.__file__)" 2>/dev/null; then
    log_ok "patched pyray importable in openpilot venv"
  else
    log_err "patched pyray not importable"
    ((errors++))
  fi

  # Check font atlases (look for generated .png files)
  if ls "${OPENPILOT_ROOT}"/selfdrive/assets/fonts/*.png >/dev/null 2>&1; then
    log_ok "font atlases present"
  else
    log_warn "font atlas .png files not found — UI text may be garbled"
  fi

  # Check critical tools
  for tool in ffmpeg git uv; do
    if command -v "${tool}" >/dev/null 2>&1; then
      log_ok "${tool}: available"
    else
      log_err "${tool}: not found"
      ((errors++))
    fi
  done

  if (( errors > 0 )); then
    log_err "Validation found ${errors} error(s). Review output above."
    return 1
  fi

  log_ok "All validation checks passed"
}

# ---------------------------------------------------------------------------
# Section 15: Summary & next steps
# ---------------------------------------------------------------------------

print_summary() {
  printf '\n'
  printf '%b%s%b\n' "${GREEN}" "============================================" "${NC}"
  printf '%b  Installation complete!%b\n' "${GREEN}" "${NC}"
  printf '%b%s%b\n' "${GREEN}" "============================================" "${NC}"
  printf '\n'
  echo "Clipper home:     ${CLIPPER_HOME}"
  echo "openpilot root:   ${OPENPILOT_ROOT}"
  echo "openpilot commit: $(cat "${OPENPILOT_ROOT}/COMMIT" 2>/dev/null || echo 'unknown')"
  printf '\n'
  echo "Quick start:"
  echo "  ./start.sh                    # Launch the web UI at http://localhost:7860"
  printf '\n'
  echo "CLI usage:"
  echo "  uv run python clip.py forward --demo   # Smoke test (video transcode)"
  echo "  uv run python clip.py ui --demo        # Full UI render"
  printf '\n'
  echo "Management:"
  echo "  ./install.sh                  # Re-run (idempotent, skips completed steps)"
  echo "  ./install.sh --uninstall      # Remove ~/.op-replay-clipper/"
  printf '\n'
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

main() {
  echo "OP Replay Clipper — Native Install"
  echo "=================================="
  echo "Target: ${CLIPPER_HOME}"
  printf '\n'

  preflight_checks
  install_system_packages
  ensure_uv
  setup_clipper_directories
  clone_openpilot
  install_openpilot_dependencies
  fix_vendored_tool_permissions
  build_openpilot_clip_dependencies
  install_patched_pyray
  generate_ui_fonts
  record_openpilot_commit
  validate_install
  print_summary
}

main "$@"
