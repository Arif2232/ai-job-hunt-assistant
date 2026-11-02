#!/bin/bash
cd "$(dirname "$0")" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
if [ -x .venv/bin/python ]; then
  exec .venv/bin/python launch.py
fi
for TASK_PYTHON in python3 python3.14 python3.13 python3.12 python3.11; do
  if command -v "$TASK_PYTHON" >/dev/null 2>&1 && "$TASK_PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    exec "$TASK_PYTHON" launch.py
  fi
done
echo "Install Python 3.11 or newer from python.org, then open this file again."
exit 1
