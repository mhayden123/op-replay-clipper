#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-7860}"

# Verify the environment is set up
if ! command -v uv >/dev/null 2>&1; then
  echo "Error: uv not found. Run ./install.sh first."
  exit 1
fi

if [[ ! -f "clip.py" ]]; then
  echo "Error: clip.py not found. Run this script from the repo root."
  exit 1
fi

# Install clipper Python deps if .venv is missing
if [[ ! -d ".venv" ]]; then
  echo "First run — installing Python dependencies..."
  uv sync
fi

echo ""
echo "  GlideKit (native)"
echo "  http://localhost:${PORT}"
echo "  Press Ctrl+C to stop"
echo ""

# Auto-open browser after a short delay (non-blocking, best-effort)
(sleep 2 && xdg-open "http://localhost:${PORT}" 2>/dev/null || true) &

exec uv run python -m uvicorn web.server:app --host 0.0.0.0 --port "${PORT}"
