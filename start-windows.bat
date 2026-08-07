@echo off
cd /d "%~dp0"
if not exist "index.html" (
  echo Could not find index.html next to this script.
  echo Open a terminal in the resume-tailor folder and run: python serve.py
  echo Then open http://localhost:8765
  pause
  exit /b 1
)
echo Resume Tailor - http://localhost:8765  (close this window to stop)
start "" http://localhost:8765
py -m http.server 8765 2>nul || python serve.py
