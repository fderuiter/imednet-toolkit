#!/usr/bin/env bash
# Bisection helper to find which pytest file creates unwanted files or state.
# Usage: bash find-polluter.sh <file_or_dir_to_check> <test_glob> [test_cmd]
# Example: bash find-polluter.sh '.git' 'tests/unit/test_*.py' pytest
set -euo pipefail

if [ $# -lt 2 ] || [ $# -gt 3 ]; then
  echo "Usage: bash $0 <file_to_check> <test_pattern> [test_cmd]"
  echo "Example: bash $0 '.coverage.tmp' 'tests/unit/test_*.py' pytest"
  exit 1
fi

POLLUTION_CHECK="$1"
TEST_PATTERN="${2#./}"
TEST_CMD="${3:-pytest -q}"

echo "Searching for test creating: $POLLUTION_CHECK"
echo "Test pattern: $TEST_PATTERN (runner: $TEST_CMD)"

TEST_FILES=$(find . \( -path "./$TEST_PATTERN" -o -path "./${TEST_PATTERN//\*\*\//}" \) | sort -u)
if [ -z "$TEST_FILES" ]; then
  TOTAL=0
else
  TOTAL=$(printf '%s\n' "$TEST_FILES" | wc -l | tr -d ' ')
fi

echo "Found $TOTAL test files"
COUNT=0
for TEST_FILE in $TEST_FILES; do
  COUNT=$((COUNT + 1))
  if [ -e "$POLLUTION_CHECK" ]; then
    echo "Pollution already exists before test $COUNT/$TOTAL ($TEST_FILE); clean first."
    exit 2
  fi
  echo "[$COUNT/$TOTAL] Testing: $TEST_FILE"
  $TEST_CMD "$TEST_FILE" > /dev/null 2>&1 || true
  if [ -e "$POLLUTION_CHECK" ]; then
    echo "FOUND POLLUTER: $TEST_FILE created $POLLUTION_CHECK"
    exit 1
  fi
done

echo "No polluter found."
exit 0
