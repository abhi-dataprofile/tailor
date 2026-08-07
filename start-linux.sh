#!/bin/bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
cd "$DIR" 2>/dev/null
if [ ! -f "index.html" ]; then
  echo "Couldn't find index.html in: $(pwd)"
  echo "cd into the resume-tailor folder, then run: python3 serve.py"
  echo "and open http://localhost:8765"
  exit 1
fi
echo "Resume Tailor → http://localhost:8765  (press Ctrl+C to stop)"
( sleep 1; xdg-open "http://localhost:8765" >/dev/null 2>&1 ) &
python3 serve.py
