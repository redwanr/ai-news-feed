#!/bin/bash
# Daily driver for the GenAI Digest pipeline, invoked by launchd
# (ops/com.redwan.genai-digest.plist) at 20:00 America/Los_Angeles.
#
# Runs the pipeline, then commits+pushes the published output. Safe to run
# manually too: `bash scripts/run_daily.sh`.
#
# BSD/macOS shell only (see SPEC.md landmine #4): no GNU-only flags, no
# `date +%s%N`, no `sed -i` without a backup suffix.

set -euo pipefail

REPO_DIR="/Users/redwan/ClaudeProjects/coding-projects/ai-news-feed"
LOG_FILE="${REPO_DIR}/state/logs/launchd.log"

mkdir -p "${REPO_DIR}/state/logs"

{
  echo "===== run_daily.sh start: $(date "+%Y-%m-%d %H:%M:%S %Z") ====="

  cd "${REPO_DIR}"

  "${REPO_DIR}/.venv/bin/python" -m digest.run

  git add docs state/ledger.jsonl

  if git diff --cached --quiet; then
    echo "run_daily.sh: no changes to commit, skipping commit/push"
  else
    git commit -m "digest: $(date +%F)"
    git push
  fi

  echo "===== run_daily.sh end: $(date "+%Y-%m-%d %H:%M:%S %Z") ====="
} >> "${LOG_FILE}" 2>&1
