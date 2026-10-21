#!/usr/bin/env bash
# Artemis wrapper (Android test automation).
# Usage:
#   sh scripts/artemis.sh doctor
#   sh scripts/artemis.sh run "Open the app and check the home screen" \
#     --profile flash --locked-app com.example.app
set -euo pipefail

export PATH="$HOME/Library/Android/sdk/platform-tools:$HOME/.local/bin:$PATH"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ARTEMIS_DIR="${ARTEMIS_DIR:-$REPO_ROOT}"

if [ -x "$ARTEMIS_DIR/.venv/bin/artemis" ]; then
  exec "$ARTEMIS_DIR/.venv/bin/artemis" "$@"
fi

if command -v uv >/dev/null 2>&1; then
  exec uv run --project "$ARTEMIS_DIR" artemis "$@"
fi

echo "Artemis not found at $ARTEMIS_DIR (no .venv, no uv)." >&2
echo "Run 'uv sync' in the repo or set ARTEMIS_DIR to another checkout." >&2
exit 1

