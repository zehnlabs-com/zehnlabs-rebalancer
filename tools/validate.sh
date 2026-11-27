#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"

echo "[1/4] Formatting and linting: no formatter or linter is configured"

echo "[2/4] Python test suite"
run_with_pythonpath() {
  local runner="$1"
  shift
  local executable
  executable="$(command -v "$runner")"
  if command -v wslpath >/dev/null 2>&1 \
    && { [[ "$executable" == /mnt/* ]] || [[ "$executable" == *.exe ]]; }; then
    env "WSLENV=${WSLENV:+$WSLENV:}PYTHONPATH/p" \
      PYTHONPATH="$repo_dir/rebalancer" "$runner" "$@"
  else
    PYTHONPATH="$repo_dir/rebalancer" "$runner" "$@"
  fi
}

python_can_run_suite() {
  run_with_pythonpath "$1" -c \
    'import pytest, fastapi, jinja2, pydantic, yaml, ably, exchange_calendars, ib_async; import app, app_config, broker_connector_base, ibkr_connector, rebalance_calculator' \
    >/dev/null 2>&1
}

uv_runner=""
for candidate in uv uv.exe; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" --version >/dev/null 2>&1; then
    uv_runner="$candidate"
    break
  fi
done

if [ -n "$uv_runner" ]; then
  run_with_pythonpath "$uv_runner" run --python 3.12 \
    --with-editable packages/app-config \
    --with-editable packages/broker-connector-base \
    --with-editable packages/rebalance-calculator \
    --with-editable packages/ibkr-connector \
    --with-requirements rebalancer/requirements.txt \
    --with-requirements rebalancer/requirements-dev.txt \
    python -m pytest -q tests
elif command -v python >/dev/null 2>&1 && python_can_run_suite python; then
  run_with_pythonpath python -m pytest -q tests
elif command -v python3 >/dev/null 2>&1 && python_can_run_suite python3; then
  run_with_pythonpath python3 -m pytest -q tests
else
  echo "uv or a Python interpreter with all declared test dependencies is required" >&2
  exit 1
fi

echo "[3/4] Docker Compose configuration"
compose_env="$(mktemp)"
trap 'rm -f "$compose_env"' EXIT
cat >"$compose_env" <<'EOF'
IB_USERNAME=validation-user
IB_PASSWORD=validation-password
ZEHNLABS_API_KEY=validation-key
DASHBOARD_ADMIN_PASSWORD=validation-dashboard-password-32
TRADING_MODE=paper
EOF
docker compose --env-file "$compose_env" config --quiet

echo "[4/4] Production image smoke test"
bash "$repo_dir/tools/smoke-test.sh"

echo "Validation passed"
