#!/usr/bin/env bash
# macOS / Linux: ./start.sh  → http://127.0.0.1:8000/ui/
set -e
cd "$(dirname "$0")"
python3 -m pip install -q -r requirements.txt
echo "ブラウザで http://127.0.0.1:8000/ui/ を開いてください（止めるときは Ctrl+C）"
python3 -m uvicorn app.main:app --port 8000
