#!/bin/bash
# Edit Twin installer: sets up the server and registers it with Claude Code.
# Run from this folder:  ./install.sh
set -e
cd "$(dirname "$0")"
HERE="$(pwd)"

echo "== Edit Twin for Claude Code"

if [ "$(uname)" != "Darwin" ]; then
  echo "!! Edit Twin needs a Mac with Final Cut Pro."; exit 1
fi

# 1. ffmpeg (pause detection, previews, loudness)
if ! command -v ffmpeg >/dev/null 2>&1 && [ ! -x /opt/homebrew/bin/ffmpeg ] && [ ! -x /usr/local/bin/ffmpeg ]; then
  if command -v brew >/dev/null 2>&1; then
    echo "Installing ffmpeg with Homebrew..."
    brew install ffmpeg
  else
    echo "!! ffmpeg not found. Install Homebrew (https://brew.sh), then run: brew install ffmpeg"
    exit 1
  fi
fi

# 2. Python 3.10+ in a private virtual environment (macOS ships 3.9)
PY=""
for c in python3.13 python3.12 python3.11 python3.10 /opt/homebrew/bin/python3 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  if command -v brew >/dev/null 2>&1; then
    brew install python@3.12
    PY="$(brew --prefix python@3.12)/bin/python3.12"
  else
    echo "!! Python 3.10 or newer is needed. Install Homebrew (https://brew.sh), then: brew install python"
    exit 1
  fi
fi
echo "Using $($PY --version)"
"$PY" -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -e '.[transcribe]'
echo "Installed into $HERE/.venv"

# 3. Register with Claude Code (available in every project)
if command -v claude >/dev/null 2>&1; then
  claude mcp remove edit-twin --scope user >/dev/null 2>&1 || true
  claude mcp add --scope user edit-twin -- "$HERE/.venv/bin/edit-twin-mcp"
  echo "Registered 'edit-twin' with Claude Code."
else
  echo "Claude Code isn't installed yet. After installing it, run:"
  echo "  claude mcp add --scope user edit-twin -- \"$HERE/.venv/bin/edit-twin-mcp\""
fi

# 4. Install (or update) the skills for Claude Code
for skill in edit-twin-rough-cut edit-twin-captions; do
  mkdir -p "$HOME/.claude/skills/$skill"
  cp "$HERE/skills/$skill/SKILL.md" "$HOME/.claude/skills/$skill/SKILL.md"
  echo "Installed the /$skill skill."
done

mkdir -p "$HOME/Movies/Edit Twin"
cat <<'MSG'

== One-time macOS permissions
System Settings > Privacy & Security:
  - Accessibility  -> turn on the app you run Claude Code in (Terminal, iTerm, VS Code...)
  - Automation     -> allow it to control "Final Cut Pro" and "System Events"
                      (macOS asks the first time; click Allow)
  - Screen & System Audio Recording -> same app, only needed for screenshots
Then quit and reopen that app.

Done! Start Claude Code (type: claude) and type /mcp to check "edit-twin" is connected.
MSG
