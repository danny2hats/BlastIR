#!/usr/bin/env bash
# BlastIR installer for Raspberry Pi OS (Bookworm / Bullseye)
#
# What it does:
#   1. Installs system packages (lirc, ir-keytable, python venv, pip)
#   2. Adds gpio-ir / gpio-ir-tx overlays to /boot/firmware/config.txt
#      (TX on GPIO 17, RX on GPIO 18 — edit IR_TX_PIN / IR_RX_PIN below to change)
#   3. Points lircd at /dev/lirc1 (the RX device) in /etc/lirc/lirc_options.conf
#   4. Creates a Python venv and installs Flask + paho-mqtt
#   5. Grants the runtime user passwordless sudo for systemctl restart ir-blaster
#   6. Installs and enables the blastir.service systemd unit
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/danny2hats/BlastIR/main/install.sh | sudo bash
#   — or —
#   git clone https://github.com/danny2hats/BlastIR.git && cd BlastIR && sudo ./install.sh
#
# After install: edit app.py and change MQTT_BROKER / MQTT_USER / MQTT_PASS
# to match your Home Assistant setup, then:
#   sudo systemctl restart blastir

set -euo pipefail

IR_TX_PIN="${IR_TX_PIN:-17}"
IR_RX_PIN="${IR_RX_PIN:-18}"
SERVICE_NAME="blastir"
REPO_URL="https://github.com/danny2hats/BlastIR.git"

# --- helpers ---
log()  { printf '\033[1;34m[blastir]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[blastir]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[blastir]\033[0m %s\n' "$*" >&2; exit 1; }

if [[ $EUID -ne 0 ]]; then
  die "Please run as root (use sudo)."
fi

# --- locate install source ---
if [[ -f "$(dirname "$0")/app.py" ]]; then
  SRC="$(cd "$(dirname "$0")" && pwd)"
  log "Installing from local checkout: $SRC"
else
  SRC="/tmp/blastir-src"
  log "Cloning $REPO_URL into $SRC"
  rm -rf "$SRC"
  git clone --depth 1 "$REPO_URL" "$SRC"
fi

# --- figure out who will run it ---
TARGET_USER="${SUDO_USER:-pi}"
if ! id -u "$TARGET_USER" >/dev/null 2>&1; then
  die "Target user '$TARGET_USER' does not exist. Re-run with SUDO_USER set."
fi
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
INSTALL_DIR="$TARGET_HOME/blastir"
log "Installing for user '$TARGET_USER' at $INSTALL_DIR"

# --- system packages ---
log "Installing system packages..."
apt-get update -qq
apt-get install -y --no-install-recommends \
  lirc ir-keytable \
  python3 python3-venv python3-pip \
  git

# --- config.txt overlays ---
CONFIG_TXT="/boot/firmware/config.txt"
[[ -f "$CONFIG_TXT" ]] || CONFIG_TXT="/boot/config.txt"
if [[ -f "$CONFIG_TXT" ]]; then
  log "Patching $CONFIG_TXT (TX=GPIO${IR_TX_PIN}, RX=GPIO${IR_RX_PIN})"
  # Remove any existing gpio-ir / gpio-ir-tx lines so we can re-add cleanly
  sed -i.blastir-bak -E '/^[[:space:]]*dtoverlay=gpio-ir(-tx)?([[:space:]]|,|$)/d' "$CONFIG_TXT"
  {
    echo ""
    echo "# Added by BlastIR installer"
    echo "dtoverlay=gpio-ir,gpio_pin=${IR_RX_PIN}"
    echo "dtoverlay=gpio-ir-tx,gpio_pin=${IR_TX_PIN}"
  } >> "$CONFIG_TXT"
else
  warn "No config.txt found — you must add the dtoverlay lines manually."
fi

# --- LIRC on /dev/lirc1 (RX) ---
if [[ -f /etc/lirc/lirc_options.conf ]]; then
  log "Pointing lircd at /dev/lirc1 (receiver)"
  sed -i.blastir-bak -E 's|^[[:space:]]*device[[:space:]]*=.*|device          = /dev/lirc1|' /etc/lirc/lirc_options.conf
  sed -i -E 's|^[[:space:]]*driver[[:space:]]*=.*|driver          = default|' /etc/lirc/lirc_options.conf
fi

# --- copy source to install dir ---
log "Copying source to $INSTALL_DIR"
install -d -o "$TARGET_USER" -g "$TARGET_USER" "$INSTALL_DIR" "$INSTALL_DIR/templates"
install -m 0644 -o "$TARGET_USER" -g "$TARGET_USER" "$SRC/app.py"                    "$INSTALL_DIR/app.py"
install -m 0644 -o "$TARGET_USER" -g "$TARGET_USER" "$SRC/templates/index.html"      "$INSTALL_DIR/templates/index.html"
install -m 0644 -o "$TARGET_USER" -g "$TARGET_USER" "$SRC/requirements.txt"          "$INSTALL_DIR/requirements.txt"
# Only seed codes.json if the user has no existing codes file
if [[ ! -f "$INSTALL_DIR/codes.json" ]]; then
  printf '{}\n' > "$INSTALL_DIR/codes.json"
  chown "$TARGET_USER:$TARGET_USER" "$INSTALL_DIR/codes.json"
fi

# --- Python venv ---
log "Creating Python venv and installing dependencies"
sudo -u "$TARGET_USER" python3 -m venv "$INSTALL_DIR/venv"
sudo -u "$TARGET_USER" "$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
sudo -u "$TARGET_USER" "$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

# --- passwordless sudo for restart-self ---
SUDOERS_FILE="/etc/sudoers.d/blastir"
log "Granting $TARGET_USER passwordless restart of the service"
cat > "$SUDOERS_FILE" <<EOF
$TARGET_USER ALL=(ALL) NOPASSWD: /bin/systemctl restart ${SERVICE_NAME}, /bin/systemctl restart ir-blaster
EOF
chmod 0440 "$SUDOERS_FILE"
visudo -cf "$SUDOERS_FILE" >/dev/null

# --- systemd unit ---
log "Installing systemd service: ${SERVICE_NAME}.service"
sed -e "s|__USER__|$TARGET_USER|g" -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
  "$SRC/systemd/blastir.service" > "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service"
systemctl enable lircd.service || true

# --- done ---
IP="$(hostname -I | awk '{print $1}')"
cat <<EOF

$(log "Installation complete.")

Next steps:
  1. Edit $INSTALL_DIR/app.py and set your MQTT broker / user / password:
        MQTT_BROKER, MQTT_PORT, MQTT_USER, MQTT_PASS, DEVICE_ID

  2. REBOOT to load the IR overlays:
        sudo reboot

  3. After reboot, the web UI will be at:
        http://${IP:-<pi-ip>}:5000

  4. Service commands:
        sudo systemctl status  ${SERVICE_NAME}
        sudo systemctl restart ${SERVICE_NAME}
        journalctl -u ${SERVICE_NAME} -f

EOF
