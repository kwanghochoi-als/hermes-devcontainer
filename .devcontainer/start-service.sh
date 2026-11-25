#!/usr/bin/env bash
set -euo pipefail

script=$(realpath "${BASH_SOURCE[0]}")
here=$(dirname "$script")
plugin_dir="$here/project-cli"
revision=$(<"$here/hermes-revision")
hermes_dir=${HERMES_HOME:-"$HOME/.hermes"}
hold="$hermes_dir/.devcontainer-release"
export PATH="$hermes_dir/hermes-agent/.hermes/bin:$HOME/.local/bin:$PATH"
runtime=${XDG_STATE_HOME:-"$HOME/.local/state"}/hermes-project-cli
mkdir -p -m 700 "$runtime"

check_revision() {
  local actual
  actual=$(git -C "$hermes_dir/hermes-agent" rev-parse --verify HEAD 2>/dev/null) || actual="읽을 수 없음"
  if [[ "$actual" != "$revision" ]]; then
    printf 'Hermes 커밋 불일치 · 지정 %s · 실제 %s\n' "$revision" "$actual" >&2
    return 1
  fi
}

check_install() {
  python3 "$here/hermes_volume.py" check >/dev/null || return 1
  check_revision || return 1
  bash "$here/setup-projects.sh" --verify
}

configured() {
  [[ -f "$runtime/configured" && "$(<"$runtime/configured")" == "$revision" ]]
}

hold_status() {
  case "$(<"$hold")" in
    verify) echo "유지보수 · 게이트웨이(Telegram)와 칸반 워커 검증 대기. resume으로 검증 후 재개하세요." ;;
    'standby '*) echo "이전 운영 볼륨 보관 중 · 복구하려면 호스트에서 볼륨을 다시 선택하세요." >&2; return 1 ;;
    *) echo "알 수 없는 Hermes 전환 상태 · $hold" >&2; return 1 ;;
  esac
}

configure() {
  local installed="$hermes_dir/plugins/project-cli"
  check_install
  mkdir -p -m 700 "$hermes_dir/plugins"
  if [[ ! -e "$installed" && ! -L "$installed" ]]; then
    ln -s "$plugin_dir" "$installed"
  elif [[ "$(realpath "$installed")" != "$plugin_dir" ]]; then
    echo "다른 project-cli 플러그인이 설치되어 있습니다: $installed" >&2
    return 1
  fi
  hermes plugins enable project-cli --no-allow-tool-override
  hermes project-cli --help >/dev/null
  hermes kanban boards create cli-workers --name "Project CLI workers" >/dev/null
  printf '%s\n' "$revision" > "$runtime/configured"
}

gateway_running() {
  local report
  report=$(hermes gateway status) || return 1
  if [[ "${1:-}" == print ]]; then printf '%s\n' "$report"; fi
  [[ "$report" == *'✓ Gateway is running'* ]]
}

lock_held() {
  [[ -f "$1" ]] && ! flock -n "$1" true
}

supervise() {
  exec 9>"$runtime/supervisor.lock"
  if [[ "${1:-}" == detached ]]; then
    flock -n 9 || return 0
  else
    flock 9
  fi
  local stopped=0 gateway_pid="" worker_pid="" guard_pid="" owns_volume=0 ready error last_error=""
  trap 'stopped=1' TERM INT
  while (( ! stopped )); do
    ready=0
    error=""
    if [[ -e "$hold" ]] && { [[ -z "$guard_pid" ]] || ! kill -0 "$guard_pid" 2>/dev/null; }; then
      python3 "$here/hermes_volume.py" _reserve "$hermes_dir" "$runtime" 8>&- 9>&- &
      guard_pid=$!
    fi
    if [[ -e "$hermes_dir/hermes-agent/.git" || -f "$runtime/configured" ]]; then
      if error=$(check_revision 2>&1); then
        if configured; then
          if [[ ! -e "$hold" ]] && { [[ -z "$guard_pid" ]] || ! kill -0 "$guard_pid" 2>/dev/null; }; then ready=1; fi
        elif [[ -f "$runtime/configured" ]]; then
          error="서비스 설정이 현재 Hermes 버전과 다릅니다. configure를 실행하세요."
        fi
      fi
    fi
    if (( ready && ! owns_volume )); then
      if error=$(check_install 2>&1); then
        exec 8>"$hermes_dir/.devcontainer-services.lock"
        if flock -n 8 && [[ ! -e "$hold" ]]; then
          owns_volume=1
          error=""
          mkdir -p -m 700 "$hermes_dir/logs"
        else
          exec 8>&-
          error="다른 서비스가 Hermes 볼륨을 사용 중이거나 검증 대기 상태입니다."
        fi
      fi
    fi
    if [[ -n "$error" && "$last_error" != "$error" ]]; then printf '%s\n' "$error" >&2; fi
    last_error=$error
    if (( owns_volume && ready )); then
      if [[ -z "$gateway_pid" ]] || ! kill -0 "$gateway_pid" 2>/dev/null; then
        # A gateway already started by a Hermes client also owns the native dispatcher.
        if ! gateway_running 8>&- 9>&-; then
          hermes gateway run --external-supervisor 8>&- 9>&- >>"$hermes_dir/logs/gateway-console.log" 2>&1 &
          gateway_pid=$!
        fi
      fi
      if [[ -z "$worker_pid" ]] || ! kill -0 "$worker_pid" 2>/dev/null; then
        hermes project-cli serve 8>&- 9>&- >>"$hermes_dir/logs/project-cli.log" 2>&1 &
        worker_pid=$!
      fi
    fi
    sleep 3 8>&- 9>&- &
    wait $! || true
  done
  for pid in "$worker_pid" "$gateway_pid" "$guard_pid"; do
    [[ -z "$pid" ]] || kill -TERM "$pid" 2>/dev/null || true
  done
  wait || true
}

ensure_supervisor() {
  nohup setsid bash "$script" supervise detached </dev/null >>"$runtime/supervisor.log" 2>&1 &
}

reserve() {
  ensure_supervisor
  local deadline=$((SECONDS + 30))
  until lock_held "$runtime/maintenance.lock"; do
    if (( SECONDS >= deadline )); then
      echo "게이트웨이·워커 잠금 확보 실패 · $runtime/supervisor.log" >&2
      return 1
    fi
    sleep 1
  done
}

start() {
  if configured; then check_install; else configure; fi
  if [[ -e "$hold" ]]; then reserve; hold_status; return; fi
  ensure_supervisor
  local deadline=$((SECONDS + 30))
  until ! lock_held "$runtime/maintenance.lock" && lock_held "$hermes_dir/cli-workers/dispatcher.lock" && gateway_running; do
    if (( SECONDS >= deadline )); then
      echo "게이트웨이 또는 칸반 워커 기동 실패 · $runtime/supervisor.log · $hermes_dir/logs" >&2
      return 1
    fi
    sleep 1
  done
}

resume() {
  check_install
  if [[ -e "$hold" ]]; then
    reserve
    (
      flock -n 8 || { echo "다른 볼륨 전환 또는 서비스가 실행 중입니다." >&2; exit 1; }
      [[ -e "$hold" ]] || exit 0
      [[ "$(<"$hold")" == verify ]] || { hold_status; exit 1; }
      if ! configured; then configure 8>&-; fi
      bash "$here/verify-hermes.sh" 8>&-
      check_install
      lock_held "$runtime/maintenance.lock" || { echo "검증 중 서비스 잠금이 해제되었습니다." >&2; exit 1; }
      rm -- "$hold"
    ) 8>"$hermes_dir/.devcontainer-services.lock"
  fi
  start
}

case "${1:-status}" in
  reserve) if [[ -e "$hold" ]]; then reserve; fi ;;
  check-revision) check_revision ;;
  configure) configure ;;
  supervise) supervise "${2:-}" ;;
  start) start ;;
  resume) resume ;;
  status)
    python3 "$here/hermes_volume.py" check >/dev/null
    check_revision
    if [[ -e "$hold" ]]; then
      hold_status
      lock_held "$runtime/maintenance.lock" || { echo "게이트웨이·워커 잠금 대기 중" >&2; exit 1; }
      exit 0
    fi
    result=0
    gateway_running print || result=1
    if lock_held "$hermes_dir/cli-workers/dispatcher.lock"; then
      echo "Kanban CLI worker running"
    else
      echo "Kanban CLI worker stopped"
      result=1
    fi
    exit "$result"
    ;;
  restart-gateway)
    check_install
    if [[ -e "$hold" ]]; then hold_status; exit; fi
    hermes gateway stop
    start
    ;;
  *) echo "usage: $0 {configure|start|resume|status|restart-gateway|supervise|reserve|check-revision}" >&2; exit 2 ;;
esac
