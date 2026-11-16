#!/usr/bin/env bash
# Human-in-the-loop reproduction loop.
# Copy this file into .scratch/, edit steps below, and run with bash.
set -euo pipefail

step() {
  printf '\n>>> %s\n' "$1"
  read -r -p "    [Enter when done] " _
}

capture() {
  local var="$1" question="$2" answer
  printf '\n>>> %s\n' "$question"
  read -r -p "    > " answer
  printf -v "$var" '%s' "$answer"
}

step "Start the reproduction target with redacted test env vars."
capture ERRORED "Did the action trigger the bug? (y/n)"
capture ERROR_MSG "Paste the redacted error message (or 'none'):"

printf '\n--- Captured ---\n'
printf 'ERRORED=%s\n' "$ERRORED"
printf 'ERROR_MSG=%s\n' "$ERROR_MSG"

