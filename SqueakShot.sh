#!/bin/bash
# SqueakShot Launcher v9.2 (macOS / Linux)
#
# Prefers an isolated conda/mamba environment ("squeakshot") built from
# environment.yml. That environment also provides ffmpeg/ffprobe, so no
# separate FFmpeg install is needed.
#
# If neither conda nor mamba is on PATH, this falls back to the old behaviour:
# pip-install flask + numpy into whatever python3 is on PATH (no isolation).
set -e
cd "$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

ENV_NAME=squeakshot

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

# Find a conda-family tool, preferring mamba (faster solver).
CONDA_TOOL=""
if command -v mamba >/dev/null 2>&1; then
    CONDA_TOOL=mamba
elif command -v conda >/dev/null 2>&1; then
    CONDA_TOOL=conda
fi

if [ -n "$CONDA_TOOL" ]; then
    # `env list` prints one env per line as "name   /path/to/envs/name".
    # Match either the leading name column or a path ending in /squeakshot.
    if ! "$CONDA_TOOL" env list | grep -qE "(^|/)${ENV_NAME}([[:space:]]|/|$)"; then
        echo "Creating conda environment '$ENV_NAME' from environment.yml..."
        echo "(first run only — this can take a minute)"
        "$CONDA_TOOL" env create -f environment.yml
    fi
    cd controller
    exec "$CONDA_TOOL" run -n "$ENV_NAME" --no-capture-output python camera_controller.py
fi

# Fallback: no conda/mamba — use the current Python directly.
echo "conda/mamba not found — using the current Python (no isolation)."
echo "For an isolated install see INSTALL.md (conda env create -f environment.yml)."
if ! python3 -c "import flask, numpy" 2>/dev/null; then
    echo "Installing controller dependencies..."
    python3 -m pip install -q -r controller/requirements.txt
fi
cd controller
exec python3 camera_controller.py
