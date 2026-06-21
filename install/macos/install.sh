#!/usr/bin/env bash
#
# macOS install script — registers the dispatch bot as a launchd user agent.
#
# Run from the repo root: bash install/macos/install.sh
#
# What this does:
#   1. Picks an install dir (default: ~/.dispatch-bot)
#   2. Copies dispatch_bot.py + your config.json + system_prompt.md there
#   3. Generates a launchd plist with paths filled in
#   4. Loads the plist via launchctl bootstrap
#
# IMPORTANT: keep the install dir OUT of ~/Documents, ~/Desktop, and other
# TCC-protected folders. /usr/bin/python3 loses Full Disk Access on Command
# Line Tools updates and won't be able to read files inside those dirs.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INSTALL_DIR="${DISPATCH_BOT_DIR:-$HOME/.dispatch-bot}"
LABEL="com.example.dispatch-bot"
PLIST_NAME="${LABEL}.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/${PLIST_NAME}"
PLIST_TEMPLATE="${REPO_DIR}/install/macos/com.example.dispatch-bot.plist"

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

mkdir -p "$HOME/Library/LaunchAgents"

# Substitute paths into the plist template.
sed \
  -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
  -e "s|__USER_HOME__|$HOME|g" \
  "$PLIST_TEMPLATE" > "$PLIST_DEST"

echo "==> Plist written to $PLIST_DEST"

# Unload any prior instance, then load fresh.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
sleep 1
launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST"

sleep 2
if launchctl list | grep -q "$LABEL"; then
  echo
  echo "==> Bot is loaded under launchd."
  echo "    Status:   launchctl list | grep $LABEL"
  echo "    Logs:     tail -f $INSTALL_DIR/launchd-stderr.log"
  echo "    Restart:  launchctl kickstart -k gui/\$(id -u)/$LABEL"
  echo "    Stop:     launchctl bootout gui/\$(id -u)/$LABEL"
  echo
  echo "Send your bot a message from Telegram to confirm end-to-end."
else
  echo
  echo "WARNING: launchctl list does not show $LABEL — check $INSTALL_DIR/launchd-stderr.log"
  exit 1
fi
