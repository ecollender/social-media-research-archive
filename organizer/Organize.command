#!/bin/bash
cd -- "$(dirname -- "$0")" || exit 1
archive_python=""
for candidate in /usr/local/bin/python3 /opt/homebrew/bin/python3; do
  if [ -x "$candidate" ]; then archive_python="$candidate"; break; fi
done
if [ -z "$archive_python" ]; then
  echo 'Install Python 3 from https://www.python.org/downloads/macos/ first.'
else
  if [ -x /usr/bin/caffeinate ]; then
    echo "Keep your Mac lid open. This launcher prevents idle sleep while the organizer runs."
    /usr/bin/caffeinate -i "$archive_python" organize.py
  else
    "$archive_python" organize.py
  fi
fi
read -r -p 'Press Return to close…' answer
