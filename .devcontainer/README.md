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

## QMD 문서 검색

Hermes의 `qmd` MCP 서버와 터미널의 `qmd`는 같은 색인을 사용합니다. 기본 컬렉션 `hermes-workspace`에는 이 운영 가이드가 들어갑니다. 문서와 검색어의 임베딩은 호스트 Ollama의 `qwen3-embedding:8b`가 생성합니다. 컨테이너에서 `http://host.docker.internal:11434/v1/embeddings`에 접근할 수 있어야 합니다.

```bash
hermes mcp test qmd
qmd query 'vec: 중단된 카드의 작업을 다시 시작하는 방법' -c hermes-workspace --no-rerank
```

기존 Hermes 대화에는 `/reload-mcp`로 도구를 불러올 수 있습니다. Hermes에는 “hermes-workspace에서 워커 재개 방법을 찾아 근거 문서와 함께 알려줘”처럼 요청하면 됩니다. 오류 코드처럼 정확한 문자열은 `qmd search`로 찾습니다. 위의 `vec:`와 `--no-rerank` 조합은 호스트 임베딩만 사용하고, 일반 `qmd query`는 질의 확장·재정렬 모델을 추가로 내려받을 수 있습니다.

프로젝트 루트에서 해당 저장소의 Markdown 문서를 추가합니다. `my-project-docs`는 프로젝트마다 고유한 이름으로 바꿉니다. 문서가 변경되면 마지막 두 명령을 다시 실행합니다.

```bash
qmd collection add "$PWD/docs" --name my-project-docs
qmd update
qmd embed
```

### 완료 카드에서 축적한 지식

`/codex`와 `/claude` 워커는 작업 중 직접 확인한 프로젝트 사실과 근거 파일·테스트를 완료 보고에 담습니다. 근거가 있는 발견이 있으면 백그라운드 워커가 완료된 실행마다 Markdown 한 파일을 저장하고, 이 프로젝트의 QMD 컬렉션을 자동 등록·갱신합니다. 문서에는 카드 ID, 워커, 완료 시각, 확인 당시의 Git HEAD(있는 경우)가 함께 기록됩니다. 차단된 작업과 발견이 없는 작업은 건너뜁니다.

새 Hermes 대화에서도 같은 프로젝트 폴더를 열고 “이전에 확인한 재시도 정책을 근거와 함께 찾아줘”처럼 요청할 수 있습니다. 터미널에서는 다음 명령으로 현재 checkout의 지식을 검색합니다. 컬렉션은 첫 발견을 저장할 때 생성되며, 다른 checkout이나 Git worktree는 별도 컬렉션을 사용합니다.

```bash
qmd query 'vec: 이전에 확인한 재시도 정책' -c "$(hermes project-cli knowledge)" --no-rerank
```

카드 완료와 색인은 독립적으로 처리됩니다. Ollama 연결이 끊기면 완료 보고와 Markdown이 남고, 워커가 30초 후 색인을 다시 시도합니다. 워커를 재시작해도 미처리 보고를 읽어 재개합니다. 성공하면 `hermes kanban --board cli-workers show <카드-ID> --json`의 `knowledge_indexed` 이벤트에 검색 가능한 문서 주소가 기록됩니다.

## 저장 위치

| 데이터 | 위치 |
|---|---|
| 프로젝트 코드와 Git worktree | 호스트와 연결한 `/workspaces` |
| Hermes 인증·설정·대화·Kanban DB | `hermes_home` 볼륨의 `/home/ubuntu/.hermes` |
| QMD 설정·문서 색인·모델 캐시 | 같은 볼륨의 `qmd/index.yml`, `qmd/index.sqlite`, `qmd/models/` |
| 완료 카드에서 확인한 프로젝트 지식 | 같은 볼륨의 `qmd/knowledge/<컬렉션>/<카드-ID>-r<실행-ID>.md` |
| CLI 세션 연결 정보·실행별 로그 | 같은 볼륨의 `cli-workers/sessions` 및 `cli-workers/runs/<카드-ID>/<시도-ID>` |
| Codex 및 Claude 원본 인증·대화 | `codex_home`, `claude_home` 볼륨 |

## 초기 설치

VS Code에서 **Dev Containers: Rebuild Container**를 실행하면 Docker Compose가 영속 볼륨을 만들거나 기존 볼륨을 연결합니다. 초기 설치가 끝나면 아래 순서로 두 CLI와 Hermes를 인증합니다.

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
