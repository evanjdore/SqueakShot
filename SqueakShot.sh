#!/bin/bash
# SqueakShot Launcher v9.0 (macOS / Linux)
set -e
cd "$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# First-run setup prompt
if [ ! -f "controller/camera_config.json" ]; then
    echo "============================================================"
    echo " First run: no config found"
    echo "============================================================"
    read -p "Run setup now? [Y/n]: " ans
    if [[ ! "$ans" =~ ^[Nn]$ ]]; then
        ./setup.sh
    else
        echo "Cancelled. Run ./setup.sh when ready."
        exit 1
    fi
fi

# Check Python deps
if ! python3 -c "import flask, numpy" 2>/dev/null; then
    echo "Installing controller dependencies..."
    python3 -m pip install -q -r controller/requirements.txt
fi

# Launch
cd controller
exec python3 camera_controller.py
