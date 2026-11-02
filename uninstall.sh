#!/usr/bin/env bash
# Remove the Linux application while preserving project state and memory.
set -euo pipefail
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
APP_DIR="$DATA/multi-agent-ai"
rm -f "$HOME/.local/bin/multi-agent-ai" "$HOME/.local/bin/multi-agent-ai-gui" "$DATA/applications/multi-agent-ai.desktop"
rm -rf "$APP_DIR/venv"
rmdir "$APP_DIR" 2>/dev/null || true
echo "Multi-Agent-AI removed. Project .multi-agent-ai state directories were not touched."
