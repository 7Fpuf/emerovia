#!/usr/bin/env bash
# Run the Agent Commons Stage 1 server.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -x .venv/bin/python ]; then
  exec .venv/bin/python -m uvicorn server.app:app --host 127.0.0.1 --port 8765
else
  exec python3 -m uvicorn server.app:app --host 127.0.0.1 --port 8765
fi
