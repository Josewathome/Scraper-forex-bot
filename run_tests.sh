#!/usr/bin/env bash
# run_tests.sh — run the full off-broker test suite.
#
# Every module under tests/test_*.py is executed as `python3 -m tests.<name>`.
# The suite needs NO MetaTrader5/pytest install: each test module sets dummy
# credentials before importing src.config and exits non-zero on any failure.
#
# Run this before every commit. Exit code 0 = all modules passed.
set -u
cd "$(dirname "$0")"

failed=0
for f in tests/test_*.py; do
    mod="${f%.py}"
    mod="${mod//\//.}"
    echo "═══ ${mod} ═══"
    if ! python3 -m "${mod}"; then
        echo "*** FAILED: ${mod}"
        failed=1
    fi
    echo
done

if [ "${failed}" -ne 0 ]; then
    echo "RESULT: TESTS FAILED"
    exit 1
fi
echo "RESULT: all test modules passed"
