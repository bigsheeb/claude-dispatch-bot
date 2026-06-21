#!/usr/bin/env bash
#
# Linux install script — registers the dispatch bot as a systemd user service.
#
# Run from the repo root: bash install/linux/install.sh
#
# What this does:
#   1. Picks an install dir (default: ~/.dispatch-bot)
#   2. Copies dispatch_bot.py + your config.json + system_prompt.md there
#   3. Generates a systemd user unit with paths filled in
#   4. Enables and starts the unit via systemctl --user

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INSTALL_DIR="${DISPATCH_BOT_DIR:-$HOME/.dispatch-bot}"
UNIT_NAME="dispatch-bot.service"
UNIT_DEST="$HOME/.config/systemd/user/${UNIT_NAME}"
UNIT_TEMPLATE="${REPO_DIR}/install/linux/dispatch-bot.service"

echo "==> Install dir: $INSTALL_DIR"
echo "==> Repo dir:    $REPO_DIR"

if [[ ! -f "$REPO_DIR/config.json" ]]; then
  echo
  echo "ERROR: $REPO_DIR/config.json does not exist."
  echo "Copy config.example.json to config.json and edit it first:"
  echo "  cp $REPO_DIR/config.example.json $REPO_DIR/config.json"
  echo "  \$EDITOR $REPO_DIR/config.json"
  exit 1
fi

if [[ ! -f "$REPO_DIR/system_prompt.md" ]]; then
  echo
  echo "ERROR: $REPO_DIR/system_prompt.md does not exist."
  echo "Copy the example:"
  echo "  cp $REPO_DIR/system_prompt.example.md $REPO_DIR/system_prompt.md"
  exit 1
fi

mkdir -p "$INSTALL_DIR"
cp "$REPO_DIR/dispatch_bot.py" "$INSTALL_DIR/"
cp "$REPO_DIR/config.json" "$INSTALL_DIR/"
cp "$REPO_DIR/system_prompt.md" "$INSTALL_DIR/"

echo "==> Files copied to $INSTALL_DIR"

mkdir -p "$HOME/.config/systemd/user"
sed \
  -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
  -e "s|__USER_HOME__|$HOME|g" \
  "$UNIT_TEMPLATE" > "$UNIT_DEST"

echo "==> Unit written to $UNIT_DEST"

systemctl --user daemon-reload
systemctl --user enable "$UNIT_NAME"
systemctl --user restart "$UNIT_NAME"

sleep 2
if systemctl --user is-active --quiet "$UNIT_NAME"; then
  echo
  echo "==> Bot is running under systemd --user."
  echo "    Status:   systemctl --user status $UNIT_NAME"
  echo "    Logs:     journalctl --user -u $UNIT_NAME -f"
  echo "    Restart:  systemctl --user restart $UNIT_NAME"
  echo "    Stop:     systemctl --user stop $UNIT_NAME"
  echo
  echo "If your session ends and you want the bot to keep running, enable lingering:"
  echo "    loginctl enable-linger \$USER"
  echo
  echo "Send your bot a message from Telegram to confirm end-to-end."
else
  echo
  echo "WARNING: $UNIT_NAME is not active. Check:"
  echo "    systemctl --user status $UNIT_NAME"
  echo "    journalctl --user -u $UNIT_NAME -n 50"
  exit 1
fi
