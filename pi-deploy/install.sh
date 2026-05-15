#!/bin/bash
# SqueakShot Pi Installer v9.1
# Reads camera_config.json from the controller directory and deploys
# sync_capture.py + camera_preview.py + camera_settings.json to each Pi,
# then installs both systemd units.

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
CONFIG_FILE="$SCRIPT_DIR/../controller/camera_config.json"

if [ ! -f "$CONFIG_FILE" ]; then
    echo "ERROR: $CONFIG_FILE not found"
    echo "Run setup.sh first to create the config."
    exit 1
fi

echo "============================================================"
echo " SQUEAKSHOT PI INSTALLER v9.1"
echo "============================================================"
echo ""
echo "Reading config from $CONFIG_FILE"

# Parse config with Python (always available in this stack)
export CFG="$CONFIG_FILE"
python3 - <<'PYEOF'
import json, os
with open(os.environ['CFG']) as f:
    cfg = json.load(f)
with open('/tmp/squeakshot_cams.tsv', 'w') as out:
    for c in cfg.get('cameras', []):
        if c.get('ip'):
            out.write(f"{c['name']}\t{c['ip']}\t{c['user']}\t{c.get('role','client')}\n")
settings = cfg.get('camera_settings', {})
settings['video_dir'] = cfg.get('video_dir', '/home/maiya/camera_videos')
with open('/tmp/squeakshot_settings.json', 'w') as out:
    json.dump(settings, out, indent=2)
server = next((c for c in cfg.get('cameras', []) if c.get('role') == 'server'), None)
with open('/tmp/squeakshot_server_ip.txt', 'w') as out:
    out.write(server['ip'] if server else '')
PYEOF

SERVER_IP=$(cat /tmp/squeakshot_server_ip.txt)
if [ -z "$SERVER_IP" ]; then
    echo "ERROR: No camera with role=server in config"
    exit 1
fi
echo "Server IP: $SERVER_IP"
echo ""

# Every ssh inside this loop MUST use -n (read stdin from /dev/null). Otherwise
# ssh inherits the while loop's stdin (the cameras.tsv file) and slurps the rest
# of it, causing only the first camera to be deployed.
while IFS=$'\t' read -r CAM_NAME CAM_IP CAM_USER CAM_ROLE; do
    echo "============================================================"
    echo " Deploying to $CAM_NAME ($CAM_ROLE) at $CAM_USER@$CAM_IP"
    echo "============================================================"

    if ! ssh -n -o ConnectTimeout=5 -o BatchMode=yes "$CAM_USER@$CAM_IP" "echo OK" >/dev/null 2>&1; then
        echo "  X SSH failed. Set up keys: ssh-copy-id $CAM_USER@$CAM_IP"
        continue
    fi
    echo "  + SSH works"

    echo "  -> Pushing sync_capture.py, camera_preview.py, camera_settings.json..."
    scp -q "$SCRIPT_DIR/sync_capture.py"   "$CAM_USER@$CAM_IP:/home/$CAM_USER/sync_capture.py"
    scp -q "$SCRIPT_DIR/camera_preview.py" "$CAM_USER@$CAM_IP:/home/$CAM_USER/camera_preview.py"
    scp -q "/tmp/squeakshot_settings.json" "$CAM_USER@$CAM_IP:/home/$CAM_USER/camera_settings.json"
    ssh -n "$CAM_USER@$CAM_IP" "chmod +x /home/$CAM_USER/sync_capture.py /home/$CAM_USER/camera_preview.py"
    echo "  + Scripts deployed"

    TMP_RECORD=$(mktemp)
    if [ "$CAM_ROLE" = "server" ]; then
        EXEC_CMD="/usr/bin/python3 /home/$CAM_USER/sync_capture.py server --config /home/$CAM_USER/camera_settings.json"
    else
        EXEC_CMD="/usr/bin/python3 /home/$CAM_USER/sync_capture.py client --server-ip $SERVER_IP --name $CAM_NAME --config /home/$CAM_USER/camera_settings.json"
    fi
    sed "s|__USER__|$CAM_USER|g; s|__EXEC__|$EXEC_CMD|g" \
        "$SCRIPT_DIR/services/squeakshot-record.service.template" > "$TMP_RECORD"

    TMP_PREVIEW=$(mktemp)
    sed "s|__USER__|$CAM_USER|g" \
        "$SCRIPT_DIR/services/squeakshot-preview.service.template" > "$TMP_PREVIEW"

    echo "  -> Installing systemd units..."
    scp -q "$TMP_RECORD"  "$CAM_USER@$CAM_IP:/tmp/squeakshot-record.service"
    scp -q "$TMP_PREVIEW" "$CAM_USER@$CAM_IP:/tmp/squeakshot-preview.service"
    ssh -n "$CAM_USER@$CAM_IP" "sudo mv /tmp/squeakshot-record.service /etc/systemd/system/ && \
                            sudo mv /tmp/squeakshot-preview.service /etc/systemd/system/ && \
                            sudo systemctl daemon-reload && \
                            sudo systemctl stop squeakshot-record squeakshot-preview 2>/dev/null || true && \
                            sudo systemctl enable squeakshot-record"
    rm -f "$TMP_RECORD" "$TMP_PREVIEW"
    echo "  + Services installed (record enabled, preview manual-start)"

    # Allow both /bin/systemctl and /usr/bin/systemctl: Bookworm sudo may resolve
    # either depending on how PATH is configured, and sudoers path matching is exact.
    SUDO_RULE="$CAM_USER ALL=(ALL) NOPASSWD: \
/bin/systemctl start squeakshot-record, /bin/systemctl stop squeakshot-record, \
/bin/systemctl start squeakshot-preview, /bin/systemctl stop squeakshot-preview, \
/bin/systemctl restart squeakshot-record, /bin/systemctl restart squeakshot-preview, \
/usr/bin/systemctl start squeakshot-record, /usr/bin/systemctl stop squeakshot-record, \
/usr/bin/systemctl start squeakshot-preview, /usr/bin/systemctl stop squeakshot-preview, \
/usr/bin/systemctl restart squeakshot-record, /usr/bin/systemctl restart squeakshot-preview"
    ssh -n "$CAM_USER@$CAM_IP" "echo '$SUDO_RULE' | sudo tee /etc/sudoers.d/squeakshot >/dev/null && sudo chmod 0440 /etc/sudoers.d/squeakshot"
    echo "  + Passwordless sudo for service control configured"
    echo ""

done < /tmp/squeakshot_cams.tsv

rm -f /tmp/squeakshot_cams.tsv /tmp/squeakshot_settings.json /tmp/squeakshot_server_ip.txt

echo "============================================================"
echo " DONE"
echo "============================================================"
echo ""
echo "Next steps:"
echo "  1. Reboot each Pi (or run: sudo systemctl start squeakshot-record)"
echo "  2. Launch controller: ./SqueakShot.sh"
echo ""
