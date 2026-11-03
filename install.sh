#!/usr/bin/env bash
# Linux Mint/Ubuntu per-user installer; modeled on ScreenCapture.
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
APP_DIR="$DATA/multi-agent-ai"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$DATA/applications"
command -v python3 >/dev/null || { echo "Python 3.11+ is required"; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { echo "Python 3.11+ is required"; exit 1; }
python3 -c 'import venv, ensurepip' || { echo "Install python3-venv"; exit 1; }
mkdir -p "$APP_DIR" "$BIN_DIR" "$DESKTOP_DIR"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/python" -m pip install --upgrade pip
"$APP_DIR/venv/bin/python" -m pip install --upgrade "$SRC"
ln -sfn "$APP_DIR/venv/bin/multi-agent-ai" "$BIN_DIR/multi-agent-ai"
cat > "$BIN_DIR/multi-agent-ai-gui" <<EOF
#!/usr/bin/env bash
exec "$APP_DIR/venv/bin/python" -m multi_agent_ai.gui "\$@"
EOF
chmod +x "$BIN_DIR/multi-agent-ai-gui"
cat > "$DESKTOP_DIR/multi-agent-ai.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Multi-Agent-AI
Comment=Agent discovery and diagnostics
Exec=$BIN_DIR/multi-agent-ai-gui
Terminal=false
Categories=Development;Utility;
EOF
command -v update-desktop-database >/dev/null && update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
echo "Installed Multi-Agent-AI. Open it from the applications menu or run multi-agent-ai-gui."
echo "CLI: $BIN_DIR/multi-agent-ai"
echo "If the GUI cannot start, install python3-tk using your distribution's package manager."
