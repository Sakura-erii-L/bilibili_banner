#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"

"$PYTHON" -m pip install -r requirements.txt
"$PYTHON" backend/capture.py
"$PYTHON" scripts/build_site.py

echo "Built: $PWD/_site"
