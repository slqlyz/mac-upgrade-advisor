#!/bin/zsh
# 双击启动 x86 Mac 硬件升级顾问 (本地 Web 界面)
cd "$(dirname "$0")"
exec python3 scripts/serve.py
