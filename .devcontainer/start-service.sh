#!/usr/bin/env bash
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"
script=$(realpath "${BASH_SOURCE[0]}")
plugin_dir="$(dirname "$script")/project-cli"
hermes_dir=${HERMES_HOME:-"$HOME/.hermes"}
runtime=${XDG_STATE_HOME:-"$HOME/.local/state"}/hermes-project-cli
mkdir -p -m 700 "$runtime"

configure() {
  local installed="$hermes_dir/plugins/project-cli"
  mkdir -p -m 700 "$hermes_dir/plugins"
  if [[ ! -e "$installed" && ! -L "$installed" ]]; then
    ln -s "$plugin_dir" "$installed"
  elif [[ "$(realpath "$installed")" != "$plugin_dir" ]]; then
    echo "다른 project-cli 플러그인이 설치되어 있습니다: $installed" >&2
    return 1
  fi
  hermes plugins enable project-cli --no-allow-tool-override
  hermes project-cli --help >/dev/null
  /usr/bin/python3 -I -B "$plugin_dir/git_auth.py" install
  hermes kanban boards create cli-workers --name "Project CLI workers" >/dev/null
  touch "$runtime/configured"
}

gateway_running() {
  local report
  report=$(hermes gateway status) || return 1
  if [[ "${1:-}" == print ]]; then printf '%s\n' "$report"; fi
  [[ "$report" == *'✓ Gateway is running'* ]]
}

supervise() {
  exec 9>"$runtime/supervisor.lock"
  if [[ "${1:-}" == detached ]]; then
    flock -n 9 || return 0
  else
    flock 9
  fi
  local stopped=0 gateway_pid="" worker_pid=""
  trap 'stopped=1' TERM INT
  mkdir -p -m 700 "$hermes_dir/logs"
  while (( ! stopped )); do
    if [[ -f "$runtime/configured" ]]; then
      if [[ -z "$gateway_pid" ]] || ! kill -0 "$gateway_pid" 2>/dev/null; then
        # A gateway already started by a Hermes client also owns the native dispatcher.
        if ! gateway_running 9>&-; then
          hermes gateway run --external-supervisor 9>&- >>"$hermes_dir/logs/gateway-console.log" 2>&1 &
          gateway_pid=$!
        fi
      fi
      if [[ -z "$worker_pid" ]] || ! kill -0 "$worker_pid" 2>/dev/null; then
        hermes project-cli serve 9>&- >>"$hermes_dir/logs/project-cli.log" 2>&1 &
        worker_pid=$!
      fi
    fi
    sleep 3 9>&- &
    wait $! || true
  done
  for pid in "$worker_pid" "$gateway_pid"; do
    [[ -z "$pid" ]] || kill -TERM "$pid" 2>/dev/null || true
  done
  wait || true
}

case "${1:-status}" in
  configure) configure ;;
  supervise) supervise "${2:-}" ;;
  start)
    [[ -f "$runtime/configured" ]] || configure
    nohup setsid bash "$script" supervise detached </dev/null >>"$runtime/supervisor.log" 2>&1 &
    ;;
  status)
    result=0
    gateway_running print || result=1
    worker_lock="$hermes_dir/cli-workers/dispatcher.lock"
    if [[ -f "$worker_lock" ]] && ! flock -n "$worker_lock" true; then
      echo "Kanban CLI worker running"
    else
      echo "Kanban CLI worker stopped"
      result=1
    fi
    exit "$result"
    ;;
  restart-gateway)
    hermes gateway stop
    bash "$script" start
    ;;
  *) echo "usage: $0 {configure|start|status|restart-gateway|supervise}" >&2; exit 2 ;;
esac
