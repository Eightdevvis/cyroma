#!/usr/bin/env bash
# Launcher for macOS / Linux. Open a Terminal, cd into this folder, run:  ./run.sh
set -e
cd "$(dirname "$0")"

if command -v uv >/dev/null 2>&1; then
  exec uv run morse.py
fi

echo "uv not found — installing it (one-time, no admin needed)…"
curl -LsSf https://astral.sh/uv/install.sh | sh

# make uv visible in this shell after install
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

if command -v uv >/dev/null 2>&1; then
  exec uv run morse.py
fi

echo "Couldn't install uv automatically. Fallback:"
echo "  python3 -m venv .venv && . .venv/bin/activate && pip install pygame numpy imageio-ffmpeg && python morse.py"
exit 1
