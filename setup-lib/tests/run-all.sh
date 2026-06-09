#!/usr/bin/env bash
#
# Run the full Task 7.1 host-test matrix: shellcheck gate + every *.test.sh.
# Aggregates a final pass/fail line. Exit 0 iff everything is green.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../.." && pwd)"
cd "$REPO_ROOT" || exit 1

fails=0

echo "=== shellcheck gate ==="
if command -v shellcheck >/dev/null 2>&1; then
    # Entrypoints with -x cover the sourced libs in context.
    if shellcheck -x setup.sh uninstall.sh; then
        echo "ok   shellcheck: setup.sh uninstall.sh (+libs via -x)"
    else
        echo "FAIL shellcheck: setup.sh/uninstall.sh"; fails=$((fails + 1))
    fi
    if shellcheck -x \
        setup-lib/tests/installer.test.sh \
        setup-lib/tests/denylist.test.sh \
        setup-lib/tests/verify-release.test.sh; then
        echo "ok   shellcheck: test scripts"
    else
        echo "FAIL shellcheck: test scripts"; fails=$((fails + 1))
    fi
else
    echo "SKIP shellcheck (not installed)"
fi

echo
echo "=== test matrix ==="
for t in verify-release.test.sh installer.test.sh denylist.test.sh; do
    echo "--- ${t} ---"
    if bash "setup-lib/tests/${t}"; then
        echo "ok   ${t}"
    else
        echo "FAIL ${t}"; fails=$((fails + 1))
    fi
    echo
done

echo "================================"
if [ "$fails" -eq 0 ]; then
    echo "ALL GREEN"
else
    echo "${fails} suite(s) FAILED"
fi
[ "$fails" -eq 0 ]
