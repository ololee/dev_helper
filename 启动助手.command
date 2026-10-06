#!/bin/zsh
set -e
cd -- "$(dirname -- "$0")"
./.venv/bin/python control.py start --open-browser
