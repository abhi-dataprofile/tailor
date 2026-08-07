#!/bin/bash
cd "$(dirname "$0")"
echo "Starting Resume Tailor + apply engine at http://localhost:8765"
open "http://localhost:8765"
python3 serve.py
