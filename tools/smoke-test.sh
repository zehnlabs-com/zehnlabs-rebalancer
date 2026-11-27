#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
stamp="${RANDOM}-$$"
image="zehnlabs-rebalancer-smoke:${stamp}"
container="zehnlabs-rebalancer-smoke-${stamp}"
volume="zehnlabs-rebalancer-smoke-${stamp}"
host_port="${SMOKE_PORT:-28778}"

if command -v python >/dev/null 2>&1 && python -c 'import urllib.request' >/dev/null 2>&1; then
  host_python=(python)
elif command -v python3 >/dev/null 2>&1 && python3 -c 'import urllib.request' >/dev/null 2>&1; then
  host_python=(python3)
elif command -v uv >/dev/null 2>&1 && uv --version >/dev/null 2>&1; then
  host_python=(uv run --python 3.12 python)
elif command -v uv.exe >/dev/null 2>&1 && uv.exe --version >/dev/null 2>&1; then
  host_python=(uv.exe run --python 3.12 python)
else
  echo "Python 3.12 or uv is required for the smoke-test HTTP probes" >&2
  exit 1
fi

run_host_python() {
  "${host_python[@]}" "$@"
}

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    docker logs "$container" >&2 || true
  fi
  docker rm -f "$container" >/dev/null 2>&1 || true
  docker rm -f "${container}-seed" >/dev/null 2>&1 || true
  docker rm -f "${container}-verify" >/dev/null 2>&1 || true
  docker volume rm "$volume" >/dev/null 2>&1 || true
  docker image rm "$image" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker build -f "$repo_dir/rebalancer/Dockerfile" -t "$image" "$repo_dir"
docker volume create "$volume" >/dev/null

# Git Bash on Windows otherwise rewrites Linux container paths in docker
# arguments (including quoted `sh -c` scripts) to host paths.
export MSYS_NO_PATHCONV=1

start_container() {
  docker run -d --name "$container" \
    -p "127.0.0.1:${host_port}:18778" \
    -e PORT=18778 \
    -e TRADING_MODE=paper \
    -e DASHBOARD_ADMIN_PASSWORD=validation-dashboard-password-32 \
    -e ZEHNLABS_API_KEY=smoke-not-a-real-key \
    -e ALLOCATIONS_BASE_URL=http://127.0.0.1:1/api/v1/strategies \
    -v "${volume}:/app/state" \
    "$image" >/dev/null
}

wait_for_ready() {
  for _ in $(seq 1 60); do
    if run_host_python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${host_port}/healthz', timeout=2).read()" >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
  run_host_python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${host_port}/healthz', timeout=5).read()"
}

wait_for_work_status() {
  run_host_python - "$host_port" "$1" <<'PY'
import http.cookies
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

port, expected = sys.argv[1:]
origin = f"http://127.0.0.1:{port}"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def open_request(request):
    try:
        return urllib.request.build_opener(NoRedirect).open(request, timeout=5)
    except urllib.error.HTTPError as error:
        if error.code == 303:
            return error
        raise


def session_cookie(response):
    parsed = http.cookies.SimpleCookie()
    parsed.load(response.headers["Set-Cookie"])
    morsel = parsed["__Host-rebalancer-session"]
    return f"{morsel.key}={morsel.value}"


login = open_request(urllib.request.Request(f"{origin}/login"))
login_html = login.read().decode("utf-8")
csrf = re.search(r'name="csrf_token" value="([^"]+)"', login_html).group(1)
anonymous_cookie = session_cookie(login)
payload = urllib.parse.urlencode(
    {
        "username": "admin",
        "password": "validation-dashboard-password-32",
        "csrf_token": csrf,
    }
).encode("ascii")
authenticated = open_request(
    urllib.request.Request(
        f"{origin}/login",
        data=payload,
        headers={"Cookie": anonymous_cookie, "Origin": origin},
    )
)
authenticated_cookie = session_cookie(authenticated)

for _ in range(60):
    work = open_request(
        urllib.request.Request(
            f"{origin}/work/smoke-work",
            headers={"Cookie": authenticated_cookie},
        )
    )
    if f"<dd>{expected}</dd>" in work.read().decode("utf-8"):
        break
    time.sleep(1)
else:
    raise SystemExit(f"Work Item did not reach {expected!r}")

for path, marker in (("/accounts", "U900001"), ("/runs", "U900001")):
    response = open_request(
        urllib.request.Request(
            f"{origin}{path}",
            headers={"Cookie": authenticated_cookie},
        )
    )
    if marker not in response.read().decode("utf-8"):
        raise SystemExit(f"Durable smoke record missing from {path}")
PY
}

start_container
wait_for_ready
run_host_python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${host_port}/login', timeout=5).read()"

# PID 1 is the Python application after gosu's exec; it must not be root.
uid="$(docker exec "$container" sh -c "awk '/^Uid:/{print \$2}' /proc/1/status")"
test "$uid" != "0"
docker exec "$container" sh -c '! command -v node >/dev/null'
docker exec "$container" sh -c 'test -f /app/state/rebalancer.db'
db_uid="$(docker exec "$container" stat -c %u /app/state/rebalancer.db)"
test "$db_uid" != "0"

# Stop the empty installation cleanly, then seed synthetic Account and Work
# records through the same typed state API used by the parent process.
docker stop --time 30 "$container" >/dev/null
docker rm "$container" >/dev/null
docker run --rm --name "${container}-seed" \
  -v "${volume}:/app/state" \
  "$image" python -c '
from app.accounts import AccountConfig
from app.results import WorkAction
from app.state.models import WorkItem, WorkSource, WorkStatus, utc_now_text
from app.state.sqlite import SQLiteState
from datetime import datetime, timedelta, timezone

state = SQLiteState("/app/state/rebalancer.db")
state.initialize()
account = AccountConfig(
    account_id="U900001",
    type="paper",
    enabled=True,
    strategy_name="smoke-strategy",
)
state.replace_accounts("paper", [account])
now = datetime.now(timezone.utc)
state.create_work_item(
    WorkItem(
        work_id="smoke-work",
        submission_id="smoke-submission",
        account_id=account.account_id,
        action=WorkAction.PREVIEW,
        source=WorkSource.DASHBOARD,
        status=WorkStatus.PENDING,
        execute_at=utc_now_text(now + timedelta(seconds=10)),
        request_token_hash="0" * 64,
        created_at=utc_now_text(now),
    )
)
'

# The shortly delayed Preview lets Uvicorn bind first, then exercises the real
# fork pool with an unreachable synthetic endpoint and produces a safe terminal Run.
start_container
wait_for_ready
wait_for_work_status completed

# ProcessPoolExecutor children are real fork workers. They inherit appuser but
# must not retain the listener or open the parent's short-lived SQLite connections.
docker exec "$container" sh -c '
workers=0
retained_listener=0
listener_inode="$(awk '\''$2 == "00000000:495A" && $4 == "0A" { print $10; exit }'\'' /proc/1/net/tcp)"
[ -n "$listener_inode" ]
for status in /proc/[0-9]*/status; do
  [ -r "$status" ] || continue
  [ "$(awk '\''$1 == "PPid:" { print $2 }'\'' "$status")" = "1" ] || continue
  case "$(awk '\''$1 == "Name:" { print $2 }'\'' "$status")" in python*) ;; *) continue ;; esac
  pid="${status#/proc/}"
  pid="${pid%/status}"
  [ "$(awk '\''$1 == "Uid:" { print $2 }'\'' "$status")" != "0" ]
  for descriptor in "/proc/${pid}/fd/"*; do
    target="$(readlink "$descriptor" 2>/dev/null || true)"
    case "$target" in
      *rebalancer.db*) exit 1 ;;
      "socket:[$listener_inode]") retained_listener=1 ;;
    esac
  done
  workers=$((workers + 1))
done
printf "fork workers=%s listener_inode=%s retained_listener=%s\n" "$workers" "$listener_inode" "$retained_listener"
[ "$workers" -ge 1 ]
[ "$retained_listener" -eq 0 ]
'

# A graceful stop must shut down the live process pool and release the bound
# port so the same production image can immediately restart on it.
docker stop --time 30 "$container" >/dev/null
test "$(docker inspect -f '{{.State.ExitCode}}' "$container")" = "0"
docker rm "$container" >/dev/null

# Inspect durable state only while the application is stopped. No second
# process opens SQLite alongside the one supported parent writer.
docker run --rm --name "${container}-verify" \
  -v "${volume}:/app/state" \
  "$image" python -c '
from app.state.models import RunStatus, WorkStatus
from app.state.sqlite import SQLiteState
s = SQLiteState("/app/state/rebalancer.db")
assert [account.account_id for account in s.list_accounts("paper")] == ["U900001"]
assert s.get_work_item("smoke-work").status == WorkStatus.COMPLETED
assert any(run.status == RunStatus.TERMINAL for run in s.list_runs(account_id="U900001"))
'

start_container
wait_for_ready
wait_for_work_status completed
