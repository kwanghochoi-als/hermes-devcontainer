# Hermes CLI 프로젝트 작업 환경

작업할 프로젝트의 터미널에서 Hermes를 엽니다. 같은 경로에서 아래 명령을 다시 실행하면 같은 이름의 프로젝트 대화를 이어갑니다.

```bash
cd /workspaces/test-prj
hermes chat --in "$PWD" --continue "project-$PWD" --create-if-missing
```

Hermes 입력창에서 각 CLI에 지시하면 `cli-workers` Kanban 보드의 카드 ID를 돌려줍니다.

```text
/codex 동기화 실패의 원인을 찾고 수정한 뒤 관련 테스트를 실행해 줘
/claude 방금 수정한 코드와 테스트 결과를 리뷰해 줘
```

`codex에게 …`와 `claude에게 …`도 Hermes의 `project_cli_submit` 도구로 같은 보드에 접수할 수 있습니다. 셸에서 직접 접수할 때는 다음 명령을 사용합니다.

```bash
hermes project-cli submit codex "동기화 실패를 수정하고 테스트해 줘"
hermes kanban --board cli-workers list
hermes kanban --board cli-workers show <카드-ID>
hermes kanban --board cli-workers runs <카드-ID>
```

## 실행과 재개

- 워커가 프로젝트의 Git checkout에서 `codex exec --json` 또는 `claude -p --verbose --output-format stream-json`을 실행합니다. 같은 checkout의 요청은 접수 순서대로 실행하고, 서로 다른 checkout은 최대 네 곳까지 병렬로 처리합니다. 독립된 변경은 별도 Git worktree에서 Hermes를 열어 맡길 수 있습니다.
- 같은 Hermes 세션·checkout·CLI의 후속 카드는 저장한 원본 CLI 세션 ID로 재개됩니다. 다른 CLI로 넘길 때는 같은 세션과 checkout에서 마지막으로 완료한 카드의 요약과 테스트 결과를 함께 전달합니다. 셸에서 같은 대화로 이어가려면 제출할 때 동일한 `--session <Hermes-세션-ID>`를 지정합니다.
- 워커가 성공적으로 종료하고 완료 보고를 반환하면 카드에 결과, 보고된 테스트, 현재 HEAD, CLI 세션 ID와 로그 경로가 기록됩니다. 승인 필요, CLI 오류, 완료 보고 누락, 실행 도중 워커 중단은 `blocked`로 처리합니다. 실제 변경과 실행별 로그를 확인한 뒤 `hermes kanban --board cli-workers unblock <카드-ID>`로 같은 작업을 다시 맡길 수 있습니다.
- 게이트웨이와 CLI 워커는 독립 프로세스입니다. 터미널을 닫거나 게이트웨이를 재시작해도 워커는 계속 실행됩니다. 컨테이너가 멈추면 다음 기동에서 대기 카드를 이어 처리하고 중단된 시도의 기록을 보존합니다.

## 메신저와 승인

Hermes의 Kanban 구독은 설정된 원래 채널·스레드로 결과를 전달합니다. 구독 여부는 Hermes의 `kanban.auto_subscribe_on_create` 설정을 따릅니다. 두 CLI는 설치된 사용자의 권한과 훅을 읽습니다. 현재 CLI에서 필요한 승인을 받을 수 없는 작업은 카드의 차단 사유와 원래 프로젝트에서 확인할 수 있습니다. 메신저에서 보류 중인 외부 CLI 명령을 직접 승인하려면 해당 CLI의 승인 훅과 메신저 연결이 별도로 필요합니다.

## 저장 위치

| 데이터 | 위치 |
|---|---|
| 프로젝트 코드와 Git worktree | 호스트와 연결한 `/workspaces` |
| Hermes 인증·설정·대화·Kanban DB | `hermes_home` 볼륨의 `/home/ubuntu/.hermes` |
| CLI 세션 연결 정보·실행별 로그 | 같은 볼륨의 `cli-workers/sessions` 및 `cli-workers/runs/<카드-ID>/<시도-ID>` |
| Codex 및 Claude 원본 인증·대화 | `codex_home`, `claude_home` 볼륨 |

## 설치와 최초 볼륨 전환

기존 컨테이너의 `~/.hermes`가 컨테이너 레이어에 있다면 **첫 재빌드 직전에** 다음 명령을 실행합니다. 기존 데이터와 SQLite 온라인 백업을 이 Compose 프로젝트의 새 볼륨에 복사합니다. 첫 재빌드 전에는 같은 컨테이너에서 다시 실행해 그동안의 변경을 반영할 수 있습니다. 볼륨 연결 후에는 복사를 건너뜁니다.

```bash
bash .devcontainer/seed-hermes-volume.sh
```

VS Code의 **Dev Containers: Rebuild Container**로 마운트를 적용합니다. 신규 설치에서는 재빌드 후 두 CLI와 Hermes의 모델을 인증합니다.

### 1. 코딩 에이전트 로그인

```bash
claude            # /login 진행 후 종료
claude auth status
```

Codex는 컨테이너 터미널에서 로그인한다. 인증 정보와 스레드는 `codex_home` 볼륨에 저장된다.

```bash
codex login --device-auth
codex login status
```

개인 계정은 ChatGPT 보안 설정에서, 조직 계정은 관리자가 워크스페이스 권한에서 장치 코드 로그인을 활성화한다. 터미널에 표시된 주소를 호스트 브라우저에서 열고 일회용 코드를 입력한다. [Codex 인증 가이드](https://developers.openai.com/codex/auth/)

### 2. Hermes 추론 모델

빈 볼륨에서 시작하고 OpenAI Codex 인증을 쓰려면 디바이스 코드로 로그인한 뒤 모델을 선택한다.

```bash
hermes auth add openai-codex --type oauth --no-browser
hermes model
hermes config get model.default
```

Claude Code의 Ultra 구독 인증은 Claude Code에서 관리한다. `CLAUDE_CODE_OAUTH_TOKEN`은 Hermes가 자체 인증에 사용하므로 `$HERMES_HOME/.env`에 넣지 않는다.

## 운영

```bash
bash .devcontainer/start-service.sh status
bash .devcontainer/start-service.sh restart-gateway
tail -f ~/.hermes/logs/project-cli.log
```

컨테이너가 시작되면 서비스 관리자가 게이트웨이와 CLI 워커를 기동합니다. VS Code 창을 닫아도 컨테이너는 실행되며 Docker 데몬 재시작에는 `unless-stopped` 정책을 적용합니다. 워커 등록을 다시 적용할 때는 `bash .devcontainer/start-service.sh configure`를 실행합니다. 실행 로그에는 프롬프트와 도구 출력이 담길 수 있어 Hermes 볼륨 안에 보관합니다.
