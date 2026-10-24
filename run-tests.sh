#!/usr/bin/env bash
#
# Export test runner for SysPerf.
#
# Validates the exported distribution end to end:
#   1. builds an isolated virtual environment,
#   2. installs the package (editable) with its dev extras,
#   3. runs the full pytest suite — unit, integration AND the export checks
#      (tests/test_export.py: completeness, version consistency, package-data,
#      shipped features),
#   4. runs ruff lint over src/ and tests/.
#
# Usage:
#   ./run-tests.sh                 # full run in a throwaway venv
#   ./run-tests.sh --no-venv       # use the current environment (CI / offline)
#   ./run-tests.sh -k trends       # extra args are forwarded to pytest
#
# Exit code is non-zero if any stage fails, so it is safe to gate a release on.
set -euo pipefail
cd "$(dirname "$0")"

USE_VENV=1
PYTEST_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --no-venv) USE_VENV=0 ;;
    *) PYTEST_ARGS+=("$arg") ;;
  esac
done

PY="${PYTHON:-python3}"

if [[ "$USE_VENV" -eq 1 ]]; then
  VENV=".venv-export-test"
  echo "== creating isolated venv ($VENV) =="
  "$PY" -m venv "$VENV"
  # shellcheck disable=SC1091
  source "$VENV/bin/activate"
  python -m pip install --quiet --upgrade pip
  echo "== installing package + dev extras =="
  python -m pip install --quiet -e ".[dev]"
  PY=python
else
  echo "== using current environment (no venv) =="
fi

echo "== pytest (unit + integration + export) =="
"$PY" -m pytest "${PYTEST_ARGS[@]}"

echo "== ruff lint =="
if "$PY" -m ruff --version >/dev/null 2>&1; then
  "$PY" -m ruff check src tests
else
  echo "ruff not installed — skipping lint (install with: pip install -e '.[dev]')"
fi

echo "All export tests passed."
