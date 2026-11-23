set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"

log()  { printf '\n\033[1;36m[startup]\033[0m %s\n' "$*"; }

log "Claude Code 설치"
command -v claude >/dev/null || curl -fsSL https://claude.ai/install.sh | bash

log "Codex CLI 설치 확인"
command -v codex >/dev/null || npm install -g @openai/codex
codex --version

log "Hermes Agent 설치"
if ! command -v hermes >/dev/null; then
  saved_launcher="${HERMES_HOME:-$HOME/.hermes}/hermes-agent/.hermes/bin/hermes"
  if [[ -x "$saved_launcher" ]]; then
    mkdir -p "$HOME/.local/bin"
    ln -s "$saved_launcher" "$HOME/.local/bin/hermes"
  else
    curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- --non-interactive
  fi
fi

log "프로젝트 생성기 연결"
bash /workspaces/.devcontainer/setup-projects.sh

log "Hermes Telegram 어댑터 설치"
hermes pm install --extra telegram

log "Hermes 웹앱 활성화 및 빌드"
hermes config set webapp.enabled true
hermes webapp --build-only

# 4. Hermes 터미널 설정
#    cwd: "." 는 홈 디렉터리로 해석되므로 작업 폴더를 명시
#    timeout: 기본 대기 600s. 600s(foreground 상한)를 넘겨 요청한 명령은
#             자동으로 백그라운드 실행 + 완료 알림으로 전환된다 (긴 claude -p 작업용)
log "Hermes 터미널 설정"
hermes config set terminal.cwd /workspaces
hermes config set terminal.timeout 600
if [ -f "$HOME/.hermes/.env" ]; then
  sed -i 's/^TERMINAL_TIMEOUT=.*/TERMINAL_TIMEOUT=600/' "$HOME/.hermes/.env"
fi

log "QMD와 호스트 Ollama 연결"
bash /workspaces/.devcontainer/setup-qmd.sh

log "Hermes Kanban CLI 워커 등록"
bash /workspaces/.devcontainer/start-service.sh configure
log "새 워커의 observer 마운트와 훅 경로 확인"
if ! hermes project-cli observer-check; then
  log "Observer 점검 실패 · Docker와 작업 폴더 연결을 확인한 후 hermes project-cli observer-check 로 재확인하세요."
fi
log "완료 — 남은 수동 단계는 .devcontainer/README.md 참고"
