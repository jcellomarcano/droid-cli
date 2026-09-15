#!/usr/bin/env bash
# Full battery of droid-cli, run by the methodology kit per commit
# (verify-commit.sh) and by hand. --full is accepted for compatibility and runs
# the same battery. CHECK_TASKS keeps the task tokens the kit maps sectors to.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
CHECK_TASKS=(
  :pytest
)
PY="${DROID_PY:-}"
if [ -z "$PY" ]; then
  if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
fi
if ! "$PY" -c 'import droid, pytest, pytest_asyncio' >/dev/null 2>&1; then
  echo "✖ $PY no tiene droid, pytest y pytest-asyncio. Instala el paquete con su extra de test: $PY -m pip install -e '.[test]'" >&2
  exit 2
fi
for task in "${CHECK_TASKS[@]}"; do
  case "$task" in
    :pytest) "$PY" -m pytest -q || { rc=$?; [ "$rc" -eq 5 ] && echo "sin tests que ejecutar todavía" || exit "$rc"; } ;;
    *) echo "unknown task $task" >&2; exit 2 ;;
  esac
done
