#!/bin/bash
# Start Packaging Studio.
# On macOS, double-clicking this file in Finder is enough.

cd "$(dirname "$0")" || exit 1

printf '\n  Starting Packaging Studio ...\n\n'

PY=""
for candidate in python3 /usr/bin/python3 /usr/local/bin/python3 /opt/homebrew/bin/python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    version=$("$candidate" -c 'import sys; print(sys.version_info[0]*100+sys.version_info[1])' 2>/dev/null)
    if [ -n "$version" ] && [ "$version" -ge 308 ]; then
      PY="$candidate"
      break
    fi
  fi
done

if [ -z "$PY" ]; then
  cat <<'MSG'

  No Python 3.8 or newer was found on this machine.

  Two ways to get it, either works:

  A) Apple's own, no download needed.
     Open the Terminal app and run:

         xcode-select --install

     Confirm the dialog that appears. It takes a few minutes.

  B) The official installer:
     https://www.python.org/downloads/macos/
     Download it, open it, click through.

  Then double-click this file again.

MSG
  read -r -p "  Press Enter to close ..." _
  exit 1
fi

exec "$PY" server.py
