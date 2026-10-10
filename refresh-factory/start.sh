#!/bin/sh
# One command to run Keystone on Linux or macOS:  ./start.sh   (options: --memory, --check, --port N). See start.py.
cd "$(dirname "$0")" || exit 1
for py in python3 python; do
  if command -v "$py" >/dev/null 2>&1; then exec "$py" start.py "$@"; fi
done
echo "Python 3.11 or newer was not found. Install it and run this again." >&2
exit 1
