#!/bin/bash
# Edit Twin installer: sets up the server and registers it with Claude Code.
# Run from this folder:  ./install.sh
#
# Speech-to-text engine (optional; the default picks for you):
#   ./install.sh                    auto: Parakeet on Apple Silicon, Whisper otherwise
#   ./install.sh --engine whisper   always Whisper; Parakeet isn't installed
#   ./install.sh --engine parakeet  always Parakeet (Apple Silicon only)
# The choice can also come from EDIT_TWIN_ASR (EDIT_TWIN_ASR=whisper ./install.sh).
# whisper/parakeet are saved with the server registration as a lock that overrides the
# user's own choice in Claude; run again with --engine auto to remove the lock.
set -e
cd "$(dirname "$0")"
HERE="$(pwd)"

ENGINE="${EDIT_TWIN_ASR:-auto}"
while [ $# -gt 0 ]; do
  case "$1" in
    --engine) ENGINE="${2:-}"; shift 2 ;;
    --engine=*) ENGINE="${1#*=}"; shift ;;
    -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "!! Unknown option: $1 (see ./install.sh --help)"; exit 1 ;;
  esac
done
ENGINE="$(echo "$ENGINE" | tr '[:upper:]' '[:lower:]')"
case "$ENGINE" in
  auto|whisper|parakeet) ;;
  *) echo "!! --engine must be auto, whisper or parakeet (got '$ENGINE')"; exit 1 ;;
esac
APPLE_SILICON=0
[ "$(uname)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ] && APPLE_SILICON=1
if [ "$ENGINE" = "parakeet" ] && [ "$APPLE_SILICON" = 0 ]; then
  echo "!! Parakeet needs an Apple Silicon Mac; use --engine auto or --engine whisper."; exit 1
fi

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
if [ "$ENGINE" = "whisper" ]; then
  EXTRAS="transcribe"
else
  EXTRAS="transcribe,parakeet"   # parakeet installs nothing on Intel Macs
fi
.venv/bin/pip install --quiet -e ".[$EXTRAS]"
if [ "$ENGINE" = "whisper" ] && .venv/bin/python -c "import parakeet_mlx" >/dev/null 2>&1; then
  .venv/bin/pip uninstall --quiet -y parakeet-mlx   # left over from an earlier install
  echo "Removed Parakeet (--engine whisper)."
fi
echo "Installed into $HERE/.venv"
if [ "$APPLE_SILICON" = 1 ]; then echo "Mac: Apple Silicon"; else echo "Mac: Intel"; fi
# the same choice the server will make, including a saved choice in style.json
CHOICE_PY='import os
from edit_twin_mcp import style, transcribe as tr
t = style.load(os.path.expanduser(os.environ.get("EDIT_TWIN_DIR", "~/Movies/Edit Twin")))["transcription"]
print(tr.describe_choice(t.get("engine"), t.get("language")))'
if [ "$ENGINE" = "auto" ]; then
  ENGINE_LINE="$(env -u EDIT_TWIN_ASR .venv/bin/python -c "$CHOICE_PY")"
else
  ENGINE_LINE="$(EDIT_TWIN_ASR="$ENGINE" .venv/bin/python -c "$CHOICE_PY")"
fi
echo "$ENGINE_LINE"
echo "  (the model downloads on the first transcription; change later with ./install.sh --engine ...)"

# 3. Register with Claude Code (available in every project)
if command -v claude >/dev/null 2>&1; then
  claude mcp remove edit-twin --scope user >/dev/null 2>&1 || true
  if [ "$ENGINE" = "auto" ]; then
    claude mcp add --scope user edit-twin -- "$HERE/.venv/bin/edit-twin-mcp"
    echo "Registered 'edit-twin' with Claude Code."
  else
    claude mcp add edit-twin --scope user -e "EDIT_TWIN_ASR=$ENGINE" -- "$HERE/.venv/bin/edit-twin-mcp"
    echo "Registered 'edit-twin' with Claude Code, locked to $ENGINE (EDIT_TWIN_ASR=$ENGINE)."
  fi
else
  LOCK=""
  [ "$ENGINE" != "auto" ] && LOCK=" -e EDIT_TWIN_ASR=$ENGINE"
  echo "Claude Code isn't installed yet. After installing it, run:"
  echo "  claude mcp add edit-twin --scope user$LOCK -- \"$HERE/.venv/bin/edit-twin-mcp\""
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
