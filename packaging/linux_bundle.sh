#!/usr/bin/env bash
set -euo pipefail
mkdir -p dist
tar -czf dist/multi-agent-ai-linux.tar.gz --transform='s,^,multi-agent-ai/,' \
  install.sh uninstall.sh pyproject.toml README.md src
echo "Created dist/multi-agent-ai-linux.tar.gz"
