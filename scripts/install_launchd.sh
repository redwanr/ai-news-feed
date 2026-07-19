#!/bin/bash
# One-time install of the launchd job that runs the digest daily at 20:00
# America/Los_Angeles. See SPEC.md landmine #5 for the launchd invocation
# details this script relies on.
#
# Usage:
#   bash scripts/install_launchd.sh
#
# This script COPIES the plist into ~/Library/LaunchAgents and calls
# `launchctl bootstrap`. It does not need to be (and should not be) run as
# part of automated verification.
#
# To uninstall later:
#   launchctl bootout gui/$(id -u)/com.redwan.genai-digest
#   rm ~/Library/LaunchAgents/com.redwan.genai-digest.plist

set -euo pipefail

REPO_DIR="/Users/redwan/ClaudeProjects/coding-projects/ai-news-feed"
LABEL="com.redwan.genai-digest"
SRC_PLIST="${REPO_DIR}/ops/${LABEL}.plist"
DEST_PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"

mkdir -p "${HOME}/Library/LaunchAgents"
mkdir -p "${REPO_DIR}/state/logs"

echo "Copying plist to ${DEST_PLIST}"
cp "${SRC_PLIST}" "${DEST_PLIST}"

echo "Bootstrapping launchd job (gui/$(id -u))"
launchctl bootstrap "gui/$(id -u)" "${DEST_PLIST}"

echo ""
echo "Installed. Verification command:"
echo "  launchctl print gui/$(id -u)/${LABEL} | head"
echo ""
echo "--- output ---"
launchctl print "gui/$(id -u)/${LABEL}" | head || true
echo "--------------"
echo ""
echo "To uninstall:"
echo "  launchctl bootout gui/$(id -u)/${LABEL}"
echo "  rm ${DEST_PLIST}"
