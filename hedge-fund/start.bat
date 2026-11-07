@echo off
REM Windows: start.bat をダブルクリック → http://127.0.0.1:8000/ui/
cd /d "%~dp0"
python -m pip install -q -r requirements.txt
start "" http://127.0.0.1:8000/ui/
python -m uvicorn app.main:app --port 8000
