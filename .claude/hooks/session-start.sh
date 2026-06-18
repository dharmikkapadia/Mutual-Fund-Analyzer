#!/bin/bash
# SessionStart hook — installs Python dependencies so Claude Code on the web
# can run the Streamlit app and the analytics modules out of the box.
set -euo pipefail

# Only run in the remote (web) environment; no-op for local CLI sessions.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-.}"

# Plain install (not --ci/--force) so re-runs reuse the cached container layer.
python3 -m pip install --quiet -r requirements.txt

echo "session-start: dependencies installed from requirements.txt"
