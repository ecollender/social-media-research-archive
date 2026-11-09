#!/bin/bash
cd -- "$(dirname -- "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo 'Install Python 3 for macOS from https://www.python.org/downloads/macos/ and try again.'
else
  python3 download.py
fi
read -r -p 'Press Return to close…' answer
