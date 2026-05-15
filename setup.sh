#!/bin/bash
# SqueakShot Setup v9.0: creates camera_config.json interactively

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
CONFIG_FILE="$SCRIPT_DIR/controller/camera_config.json"
EXAMPLE_FILE="$SCRIPT_DIR/controller/camera_config.example.json"

echo "============================================================"
echo " SQUEAKSHOT SETUP v9.0"
echo "============================================================"
echo ""

# If config exists, ask about overwriting
if [ -f "$CONFIG_FILE" ]; then
    read -p "Config exists. Overwrite? (y/N) " ans
    [[ "$ans" =~ ^[Yy]$ ]] || { echo "Cancelled."; exit 0; }
fi

# Number of cameras
read -p "How many cameras? [3]: " NUM_CAMS
NUM_CAMS=${NUM_CAMS:-3}

# Default SSH user
read -p "Default SSH username on the Pis [maiya]: " DEFAULT_USER
DEFAULT_USER=${DEFAULT_USER:-maiya}

# Collect camera info
CAMS_JSON=""
for i in $(seq 0 $((NUM_CAMS - 1))); do
    echo ""
    echo "--- Camera $i ---"
    if [ "$i" -eq 0 ]; then
        ROLE="server"
        echo "  Role: server (cam0 always serves)"
    else
        ROLE="client"
        echo "  Role: client"
    fi
    read -p "  Name [cam$i]: " CAM_NAME
    CAM_NAME=${CAM_NAME:-cam$i}
    read -p "  IP address: " CAM_IP
    read -p "  SSH user [$DEFAULT_USER]: " CAM_USER
    CAM_USER=${CAM_USER:-$DEFAULT_USER}

    if [ -n "$CAMS_JSON" ]; then
        CAMS_JSON+=","
    fi
    CAMS_JSON+="
    {
      \"name\": \"$CAM_NAME\",
      \"ip\": \"$CAM_IP\",
      \"user\": \"$CAM_USER\",
      \"role\": \"$ROLE\"
    }"
done

# Video dir on Pis
echo ""
read -p "Remote video directory on Pis [/home/$DEFAULT_USER/camera_videos]: " VIDEO_DIR
VIDEO_DIR=${VIDEO_DIR:-/home/$DEFAULT_USER/camera_videos}

# Local video dir
DEFAULT_LOCAL="$HOME/SqueakShot_Videos"
read -p "Local video directory [$DEFAULT_LOCAL]: " LOCAL_DIR
LOCAL_DIR=${LOCAL_DIR:-$DEFAULT_LOCAL}

# Camera settings
echo ""
echo "Camera settings (press Enter for defaults):"
read -p "  Output width [1536]: " OUT_W
OUT_W=${OUT_W:-1536}
read -p "  Output height [864]: " OUT_H
OUT_H=${OUT_H:-864}
read -p "  Framerate [56]: " FPS
FPS=${FPS:-56}
read -p "  Bitrate Mbps [25]: " BITRATE
BITRATE=${BITRATE:-25}

# Write config
cat > "$CONFIG_FILE" <<EOF
{
  "cameras": [$CAMS_JSON
  ],
  "video_dir": "$VIDEO_DIR",
  "local_video_dir": "$LOCAL_DIR",
  "camera_settings": {
    "output_width": $OUT_W,
    "output_height": $OUT_H,
    "sensor_width": 2304,
    "sensor_height": 1296,
    "framerate": $FPS,
    "bitrate_mbps": $BITRATE
  }
}
EOF

echo ""
echo "============================================================"
echo " CONFIG WRITTEN: $CONFIG_FILE"
echo "============================================================"
echo ""

# Validate
python3 -c "import json; json.load(open('$CONFIG_FILE'))" && echo "+ Valid JSON"

echo ""
echo "Next steps:"
echo "  1. Set up SSH keys to each Pi (if not done): ssh-copy-id user@ip"
echo "  2. Deploy services to Pis:    cd pi-deploy && ./install.sh"
echo "  3. Launch controller:         ./SqueakShot.sh"
echo ""
