# Hermes CLI 프로젝트 작업 환경

웹앱에서 **새 프로젝트**를 누르고 이름에 `test-prj`를 입력해 생성합니다. 폴더를 비워 두면 `/workspaces/test-prj`에 Git 저장소가 생깁니다. 같은 워크스페이스 안에 기존 저장소가 있으면 기본 폴더로 선택할 수 있습니다. 터미널에서도 같은 생성기를 사용합니다.

```bash
hermes project create test-prj --use
cd /workspaces/test-prj
hermes chat --in "$PWD" --continue "test-prj-세션"
```

생성기는 프로젝트에 `test-prj` Kanban 보드, `test-prj-세션`이라는 시작 대화 한 개, 프로젝트 전용 QMD 컬렉션을 연결합니다. 첫 커밋이 없으면 빈 커밋을 만들어 Kanban이 바로 Git worktree를 열 수 있게 합니다. 같은 이름과 폴더로 다시 생성하면 기존 항목을 사용합니다. 시작 대화는 웹앱 프로젝트 목록에서도 열 수 있습니다.

Hermes 입력창에서 각 CLI에 작업을 맡기면 프로젝트 보드의 카드 ID가 돌아옵니다. 생성기로 등록하지 않은 폴더의 작업은 공용 `cli-workers` 보드에 접수됩니다.

```text
/codex 동기화 실패의 원인을 찾고 수정한 뒤 관련 테스트를 실행해 줘
/claude 방금 수정한 코드와 테스트 결과를 리뷰해 줘
```

`codex에게 …`와 `claude에게 …`도 Hermes의 `project_cli_submit` 도구로 같은 보드에 접수할 수 있습니다. 셸에서 직접 접수할 때는 다음 명령을 사용합니다.

```bash
hermes project-cli submit codex "동기화 실패를 수정하고 테스트해 줘"
hermes project-cli board
hermes kanban --board test-prj list
hermes kanban --board test-prj show <카드-ID>
hermes kanban --board test-prj runs <카드-ID>
```

## 실행과 재개

- 일반 프로젝트에서 워커는 `codex exec --dangerously-bypass-approvals-and-sandbox` 또는 `claude -p --dangerously-skip-permissions`로 실행합니다. 새 카드와 세션 재개에 같은 옵션을 적용합니다. Pharmmate 관찰 전용 보드는 대화형 승인 없이 원격 `main`의 고정된 커밋만 조회하는 전용 실행기를 사용합니다.
- 같은 checkout의 요청은 접수 순서대로 실행하고, 서로 다른 checkout은 최대 네 곳까지 병렬로 처리합니다. 독립된 변경은 별도 Git worktree에서 Hermes를 열어 맡길 수 있습니다.
- 같은 Hermes 세션·checkout·CLI의 후속 카드는 저장한 원본 CLI 세션 ID로 재개됩니다. 다른 CLI로 넘길 때는 같은 세션과 checkout에서 마지막으로 완료한 카드의 요약과 테스트 결과를 함께 전달합니다. 셸에서 같은 대화로 이어가려면 제출할 때 동일한 `--session <Hermes-세션-ID>`를 지정합니다.
- 워커가 성공적으로 종료하고 완료 보고를 반환하면 카드에 결과, 보고된 테스트, 현재 HEAD, CLI 세션 ID와 로그 경로가 기록됩니다. 승인 필요, CLI 오류, 완료 보고 누락, 실행 도중 워커 중단은 `blocked`로 처리합니다. 실제 변경과 실행별 로그를 확인한 뒤 `hermes kanban --board test-prj unblock <카드-ID>`로 같은 작업을 다시 맡길 수 있습니다.
- 게이트웨이와 CLI 워커는 독립 프로세스입니다. 터미널을 닫거나 게이트웨이를 재시작해도 워커는 계속 실행됩니다. 컨테이너가 멈추면 다음 기동에서 대기 카드를 이어 처리하고 중단된 시도의 기록을 보존합니다.

## Pharmmate 백그라운드 Git 읽기 인증

VS Code를 닫은 뒤에도 Hermes가 Pharmmate 리뷰를 시작하려면 devcontainer가 실행 중이어야 합니다. GitHub에서 fine-grained personal access token을 만들 때 소유자로 `alssolution`을 선택합니다. **Only select repositories**에서 `pharmmate`, `pharmmate-als`, `gitops-als`, `infra-als`, `jenkins-als`, `jenkins-lib`를 선택하고 **Contents** 권한을 **Read-only**로 지정합니다. 조직에서 승인을 요구하면 승인 후 등록합니다. [GitHub 토큰 생성](https://github.com/settings/personal-access-tokens/new?name=Hermes+Pharmmate&target_name=alssolution&contents=read) · [GitHub 권한 안내](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)

```bash
hermes project-cli git-auth setup
hermes project-cli git-auth status
```

`setup`의 숨김 입력란에 토큰을 붙여 넣습니다. 여섯 원격의 `main`을 모두 확인하면 토큰을 영속 볼륨의 `~/.hermes/git-auth/pharmmate.token`에 권한 `0600`으로 저장합니다. 전용 Git 인증은 `~/.cache/pharmmate-observer/`의 미러에 적용되며, 실행 중인 게이트웨이와 워커도 다음 Git 호출부터 사용합니다. 토큰 갱신도 같은 `setup` 명령으로 진행합니다.

## Telegram에서 승인·답변·작업 보고 받기

Telegram에서 **@BotFather**로 봇을 만들고, 만든 봇과의 개인 대화에 `/start`를 한 번 보내 대화를 엽니다. 본인의 숫자 사용자 ID가 필요하면 별도로 **@userinfobot**에 `/start`를 보내 답장의 `Id`를 확인합니다. 컨테이너 터미널에서 아래 설정 명령을 실행하고 BotFather의 봇 토큰과 본인의 숫자 ID를 차례로 입력합니다. [사용자 ID 확인](https://t.me/userinfobot)

Hermes 대시보드의 **Messaging → Telegram → Create with QR**로 봇을 연결한 경우에는 등록된 토큰과 계정을 재사용할 수 있습니다. [Hermes Telegram 연결 안내](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/telegram)

```bash
hermes project-cli telegram setup
hermes project-cli telegram status
```

설정 명령은 Telegram에서 봇과 개인 대화를 확인한 뒤 운영자를 등록하고 게이트웨이를 재시작합니다. 봇에 `/workstatus`를 보내 연결을 확인합니다. 토큰과 수신 설정은 컨테이너의 `~/.hermes` 볼륨에 저장됩니다.

| DM에 온 메시지 | 처리 방법 |
| --- | --- |
| Hermes 명령 승인 | 실제 명령과 사유를 확인하고 승인 또는 거절 버튼을 누릅니다. `/workok <ID>`는 해당 명령을 한 번 승인하고 `/workno <ID>`는 거절합니다. 유효 시간은 메시지에 표시됩니다. |
| 로컬 Hermes 세션의 질문 | `/workreply <ID> <답변>`으로 원래 세션에 답합니다. 선택지가 있으면 번호, 복수 선택이면 `1,3`처럼 답할 수 있습니다. |
| Codex·Claude 의사결정 요청 | 프로젝트, 카드, 제안한 작업과 영향을 확인합니다. `/workok <ID>`는 그 제안을 승인하고, `/workreply <ID> <답변>`은 선택한 내용으로 카드를 재개합니다. `/workno <ID>`로 거절하면 카드가 보류됩니다. |
| Codex·Claude 실행 오류 | CLI 실행 실패, 완료 보고 누락, 워커 중단은 원인과 함께 보고합니다. 원인을 해결한 뒤 해당 보드에서 카드를 재개할 수 있습니다. |
| 완료·실행 중단·검토 요청 | 프로젝트 보드와 공용 `cli-workers` 보드의 새 보고를 자동으로 받습니다. 완료 보고에는 워커가 기록한 테스트 결과가 들어갑니다. 사람의 입력을 요청한 로컬 Hermes 턴도 종료 상태와 세션 ID를 보냅니다. |

워커는 로컬 코드 수정, 빌드, 테스트와 복구 가능한 오류를 스스로 처리합니다. 요구사항만으로 결정할 수 없는 중요한 선택이나, 운영 배포·외부 게시·영구 삭제·결제·접근 권한 변경에 추가 승인이 필요할 때는 실행 전에 카드로 묻습니다. 운영자 답변은 해당 카드의 제안에 한 번 반영됩니다. 새로운 결정에는 새 요청 ID가 부여됩니다. 게이트웨이를 재시작하면 마지막 전송 위치부터 이어갑니다. Hermes의 `sudo` 비밀번호는 원래 작업 화면에서 입력합니다.

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

새 Hermes 대화에서도 같은 프로젝트 폴더를 열고 “이전에 확인한 재시도 정책을 근거와 함께 찾아줘”처럼 요청할 수 있습니다. 프로젝트의 등록 폴더와 연결된 Git worktree는 프로젝트 생성 시 준비된 컬렉션을 함께 사용합니다. 미등록 checkout에는 첫 발견을 저장할 때 개별 컬렉션이 생깁니다. 터미널에서는 다음 명령으로 검색합니다.

```bash
qmd query 'vec: 이전에 확인한 재시도 정책' -c "$(hermes project-cli knowledge)" --no-rerank
```

카드 완료와 색인은 독립적으로 처리됩니다. Ollama 연결이 끊기면 완료 보고와 Markdown이 남고, 워커가 30초 후 색인을 다시 시도합니다. 워커를 재시작해도 미처리 보고를 읽어 재개합니다. 성공하면 `hermes kanban --board test-prj show <카드-ID> --json`의 `knowledge_indexed` 이벤트에 검색 가능한 문서 주소가 기록됩니다.

## 저장 위치

| 데이터 | 위치 |
|---|---|
| 프로젝트 코드와 Git worktree | 호스트와 연결한 `/workspaces` |
| Hermes 인증·설정·대화·Kanban DB | `hermes_home` 볼륨의 `/home/ubuntu/.hermes` |
| QMD 설정·문서 색인·모델 캐시 | 같은 볼륨의 `qmd/index.yml`, `qmd/index.sqlite`, `qmd/models/` |
| 완료 카드에서 확인한 프로젝트 지식 | 같은 볼륨의 `qmd/knowledge/<컬렉션>/`에 보드·카드·실행별 Markdown |
| CLI 세션 연결 정보·실행별 로그 | 같은 볼륨의 `cli-workers/sessions` 및 `cli-workers/boards/<보드>/runs/`. 공용 보드의 로그는 `cli-workers/runs/` |
| Codex 및 Claude 원본 인증·대화 | `codex_home`, `claude_home` 볼륨 |

## 초기 설치

VS Code에서 **Dev Containers: Rebuild Container**를 실행하면 Docker Compose가 영속 볼륨을 만들거나 기존 볼륨을 연결합니다. 초기 설치가 끝나면 아래 순서로 두 CLI와 Hermes를 인증합니다.

초기 설치는 `.devcontainer/setup-projects.sh`에서 Hermes 기본 생성기와 웹앱 폼에 연동 패치를 적용한 다음 웹앱을 빌드합니다.

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
