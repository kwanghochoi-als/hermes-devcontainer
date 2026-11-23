# Hermes CLI 프로젝트 작업 환경

웹앱에서 **새 프로젝트**를 누르고 이름에 `test-prj`를 입력해 생성합니다. 폴더를 비워 두면 `/workspaces/test-prj`에 Git 저장소가 생깁니다. 같은 워크스페이스 안에 기존 저장소가 있으면 기본 폴더로 선택할 수 있습니다. 터미널에서도 같은 생성기를 사용합니다.

```bash
hermes project create test-prj --use
cd /workspaces/test-prj
hermes chat --in "$PWD" --continue "test-prj-세션"
```

생성기는 프로젝트에 `test-prj` Kanban 보드, `test-prj-세션`이라는 시작 대화 한 개, 프로젝트 전용 QMD 컬렉션을 연결합니다. 첫 커밋이 없으면 빈 커밋을 만들어 Kanban이 바로 Git worktree를 열 수 있게 합니다. 같은 이름과 폴더로 다시 생성하면 기존 항목을 사용합니다. 시작 대화는 웹앱 프로젝트 목록에서도 열 수 있습니다.

### 프로젝트 삭제

웹앱의 프로젝트 목록에서 등록한 프로젝트 행의 **⋯ → Delete project**를 누르고 대상 이름을 확인합니다. 터미널에서는 `hermes project delete <프로젝트 ID 또는 슬러그>`를 실행하고 확인창에 슬러그를 입력합니다. 자동화에서는 같은 명령에 `--yes`를 붙입니다.

삭제하면 프로젝트 등록, 전용 칸반의 카드·이력·첨부 파일, 프로젝트의 Hermes 대화와 전용 워커 기록, QMD 학습 문서·색인·임베딩을 영구 삭제합니다. 해당 카드의 Telegram 승인 요청도 만료됩니다. 소스 폴더, Git 저장소, 연결된 worktree와 미커밋 파일은 보존합니다. 실행 중인 카드나 세션이 있으면 표시된 작업을 마친 뒤 삭제할 수 있습니다.

같은 이름과 경로로 `hermes project create`를 다시 실행하면 기존 Git 저장소에 새 시작 세션과 빈 QMD 컬렉션을 연결합니다. 새 칸반에는 `test-prj-2`처럼 이전 보드와 구별되는 이름을 부여합니다. 프로젝트 폴더에서 `hermes project-cli board`로 확인할 수 있습니다.

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
- 워커는 프로젝트와 CLI마다 인증·세션용 홈을 두고 카드마다 별도 Docker 컨테이너를 실행합니다. 일반 카드에는 선택한 checkout, 연결된 worktree의 공통 Git 메타데이터, 해당 카드의 첨부파일을 연결합니다. 워커에서는 컨트롤러의 Docker 소켓, Hermes DB, 다른 프로젝트 홈에 접근할 수 없습니다. 운영 저장소 자체에 작업을 위임할 때는 `/workspaces`의 별도 Git worktree를 엽니다.
- 같은 checkout에서는 한 번에 한 카드를 접수 순서대로 실행합니다. 기본 병렬 한도는 전체 4개, 프로젝트당 2개입니다. 별도 worktree에 접수하면 같은 프로젝트에서도 두 작업을 진행할 수 있습니다. 기본 실행 한도는 24시간이며, Kanban 카드에 지정한 `--max-runtime`이 우선합니다. Compose 환경 변수 `HERMES_CLI_CONCURRENCY`, `HERMES_CLI_PROJECT_CONCURRENCY`, `HERMES_CLI_MAX_RUNTIME_SECONDS`로 조정할 수 있습니다. 실행 시간에 0을 지정하면 시간제한이 해제됩니다.
- 위임을 접수하고 워커가 카드를 가져갈 때 보드의 프로젝트 ID, 폴더 소유자, Git worktree의 원본을 검증합니다. 다른 프로젝트를 가리키는 수동 생성 카드는 원인을 기록하고 차단됩니다.
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

## Telegram에서 승인·자료 요청·환경 조치 처리하기

Telegram에서 **@BotFather**로 봇을 만들고, 만든 봇과의 개인 대화에 `/start`를 한 번 보내 대화를 엽니다. 본인의 숫자 사용자 ID가 필요하면 별도로 **@userinfobot**에 `/start`를 보내 답장의 `Id`를 확인합니다. 컨테이너 터미널에서 아래 설정 명령을 실행하고 BotFather의 봇 토큰과 본인의 숫자 ID를 차례로 입력합니다. [사용자 ID 확인](https://t.me/userinfobot)

Hermes 대시보드의 **Messaging → Telegram → Create with QR**로 봇을 연결한 경우에는 등록된 토큰과 계정을 재사용할 수 있습니다. [Hermes Telegram 연결 안내](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/telegram)

```bash
hermes project-cli telegram setup
hermes project-cli telegram status
```

설정 명령은 Telegram에서 봇과 개인 대화를 확인한 뒤 운영자를 등록하고 게이트웨이를 재시작합니다. 토큰과 수신 설정은 컨테이너의 `~/.hermes` 볼륨에 저장됩니다.

| 운영자 개인 대화 명령 | 결과 |
| --- | --- |
| `/workstatus` | 모든 활성 프로젝트와 공용 보드에서 승인 대기·자료 대기·환경 조치 대기를 구분해 봅니다. 실행 중인 CLI 카드에는 마지막 출력 이후 경과 시간도 표시됩니다. |
| `/workstatus test-prj` | 해당 보드의 미완료 카드 ID, 제목, 상태를 봅니다. |
| `/workrequest test-prj <카드-ID>` | 해당 카드에서 응답을 기다리는 요청을 같은 ID로 다시 받습니다. Telegram 연결 전에 차단된 카드도 조회할 수 있습니다. |

| DM에 온 메시지 | 처리 방법 |
| --- | --- |
| 승인 요청 | 대상과 영향을 확인한 뒤 **승인** 또는 **거절**을 누릅니다. `/workok <ID>`는 제안된 작업 한 건을 승인합니다. Codex·Claude 카드에는 `/workreply <ID> <지시>`로 허용 범위를 지정할 수도 있습니다. |
| 자료 요청 | 워커에 필요한 파일의 프로젝트 내 경로, 참고 링크, 선택지 또는 답을 `/workreply <ID> <내용>`으로 전달합니다. 카드의 워커나 질문을 보낸 Hermes 세션이 답을 받습니다. 로컬 Hermes 질문의 복수 선택은 `1,3`처럼 답합니다. |
| 환경 조치 요청 | 안내된 devcontainer 또는 호스트에서 인증·Docker 연결 등 원인을 조치한 뒤 **조치 완료 · 재시도** 또는 `/workfixed <ID>`를 선택합니다. 같은 카드가 실행 조건을 다시 확인합니다. 토큰은 안내된 로컬 설정 화면에서 입력합니다. Hermes가 원래 화면에서 sudo 입력을 기다릴 때는 그 화면에서 입력하면 진행됩니다. |
| 작업 실행 오류 | 완료 보고 누락 등 구체적인 운영자 조치가 확인되지 않은 실패는 원인과 로그를 보고합니다. 로그를 점검한 뒤 해당 보드에서 카드를 재개할 수 있습니다. |
| 완료·실행 중단·검토 요청 | 프로젝트 보드와 공용 `cli-workers` 보드의 새 보고를 자동으로 받습니다. 완료 보고에는 워커가 기록한 테스트 결과가 들어갑니다. 사람의 입력을 요청한 로컬 Hermes 턴도 종료 상태와 세션 ID를 보냅니다. |

워커는 로컬 코드 수정, 빌드, 테스트와 복구 가능한 오류를 스스로 처리합니다. 운영자의 응답은 알림에 표시된 보드·카드의 해당 요청에 한 번만 반영됩니다. 카드 요청은 `/workno <ID>`로 보류할 수 있습니다. 잘못된 종류의 명령을 보내면 요청은 응답 대기 상태를 유지합니다. 유형 기록이 없는 기존의 사람 확인 카드는 자료 요청으로 표시되며, `/workreply`로 구체적인 지시를 주면 재개됩니다. 게이트웨이를 재시작하면 마지막 전송 위치부터 이어갑니다.

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

카드를 실행할 때 Hermes는 제목과 본문으로 해당 프로젝트의 이전 완료 문서를 검색해, 관련 문서 최대 3개의 발췌와 출처를 워커에게 전달합니다. 새 세션, 다른 CLI, 등록 프로젝트의 연결된 Git worktree에서도 같은 지식을 활용합니다. 워커는 문서에 기록된 과거 커밋을 참고해 현재 코드에서 근거를 다시 확인합니다. Pharmmate에서는 이번 실행이 검증한 원격 스냅샷으로 확인합니다.

검색에는 QMD 키워드 색인과 호스트 Ollama 임베딩을 사용합니다. Ollama가 응답하지 않으면 키워드 검색 결과를 사용하고, 색인을 사용할 수 없거나 관련 근거가 없으면 카드 내용으로 바로 진행합니다. 완료된 시도에 전달한 문서 주소는 Kanban 실행 메타데이터의 `knowledge_references`에서 확인할 수 있습니다.

Hermes의 QMD 질의와 셸의 `qmd search`, `query`, `vsearch`는 현재 프로젝트의 지식을 기본으로 검색합니다. 연결된 Git worktree도 같은 컬렉션을 사용합니다. 운영 폴더에서 검색하면 운영 가이드를 조회합니다. 전체 지식과 별도 등록한 문서를 함께 찾으려면 셸에서는 `--all-projects`, Hermes QMD 도구에서는 `collections: ["*"]`를 지정합니다. 특정 문서 컬렉션은 `-c <이름>` 또는 MCP의 `collections`로 직접 선택합니다.

```bash
qmd query 'vec: 이전에 확인한 재시도 정책' --no-rerank
qmd search '재시도 정책' --all-projects
```

카드 완료와 색인은 독립적으로 처리됩니다. Ollama 연결이 끊기면 완료 보고와 Markdown이 남고, 워커가 30초 후 색인을 다시 시도합니다. 워커를 재시작해도 미처리 보고를 읽어 재개합니다. 성공하면 `hermes kanban --board test-prj show <카드-ID> --json`의 `knowledge_indexed` 이벤트에 검색 가능한 문서 주소가 기록됩니다.

Pharmmate의 Codex 워커는 `pharmmate_snapshot` 조회 도구로 여섯 원격의 고정된 `main` 커밋에서 파일 목록과 내용을 읽습니다. 워커는 분석을 시작하기 전에 자가 검사 세션에서 이 도구의 호출이 훅으로 차단되는지 확인합니다. 실제 분석 세션이 발급받은 스냅샷으로만 조회할 수 있습니다.

Pharmmate 관찰이 완료되면 그 CLI 세션이 실제로 읽을 수 있었던 원격 6곳의 저장소 이름과 `main` 커밋 SHA를 Kanban 실행의 `source_commits`와 QMD 문서에 기록합니다. 이 경우에는 별도 지식 항목이 없어도 출처 문서를 만듭니다.

## 저장 위치

운영 저장소는 최상위 폴더 중 `.devcontainer/`와 `.github/`를 추적합니다. `/workspaces/<프로젝트>`의 소스와 로컬 작업 파일은 각 프로젝트에서 커밋합니다. 운영 파일 정리 대상을 미리 확인하려면 `git clean -nd -- .devcontainer/ .github/`를 사용합니다.

| 데이터 | 위치 |
|---|---|
| 프로젝트 코드와 Git worktree | 호스트와 연결한 `/workspaces` |
| Hermes 인증·설정·대화·Kanban DB | `hermes_home` 볼륨의 `/home/ubuntu/.hermes` |
| QMD 설정·문서 색인·모델 캐시 | 같은 볼륨의 `qmd/index.yml`, `qmd/index.sqlite`, `qmd/models/` |
| 완료 카드에서 확인한 프로젝트 지식 | 같은 볼륨의 `qmd/knowledge/<컬렉션>/`에 보드·카드·실행별 Markdown |
| CLI 세션 연결 정보·실행별 로그 | 같은 볼륨의 `cli-workers/sessions` 및 `cli-workers/boards/<보드>/runs/`. 공용 보드의 로그는 `cli-workers/runs/` |
| 컨트롤러 CLI 로그인 | `codex_home`, `claude_home` 볼륨. 로그인 갱신과 기본 모델 선택은 다음 작업에서 워커에 적용됩니다. |
| 격리 워커의 프로젝트별 CLI 홈·대화 | `hermes_home` 볼륨의 `cli-workers/homes/`. 전환 이전 세션을 재개할 때는 해당 세션의 대화 파일을 가져옵니다. |

## 초기 설치

VS Code에서 **Dev Containers: Rebuild Container**를 실행하면 Docker Compose가 영속 볼륨을 만들거나 기존 볼륨을 연결합니다. 초기 설치가 끝나면 아래 순서로 두 CLI와 Hermes를 인증합니다.

초기 설치는 `.devcontainer/setup-projects.sh`에서 Hermes 기본 생성기와 웹앱 폼에 연동 패치를 적용한 다음 웹앱을 빌드합니다.

### 새 호스트에서 프로젝트 복원

운영 저장소에는 `.devcontainer/projects.json`의 상대 경로·원격 목록과 `.devcontainer/observer/`의 Pharmmate 관찰 정책이 들어 있습니다. 초기 기동에서는 프로젝트 데이터를 만들지 않습니다. 다음 명령은 지정한 프로젝트의 Git 루트, 보드, 시작 세션, QMD 컬렉션을 준비합니다. `--clone`을 붙이면 현재 Git 자격 증명으로 목록에서 빠진 checkout을 내려받습니다. 같은 origin의 기존 checkout은 그대로 사용하며, 다른 origin이 있으면 해당 경로를 표시하고 중단합니다.

```bash
hermes project-cli restore pharmmate
hermes project-cli restore pharmscope --clone
```

Pharmmate 관찰은 `git-auth setup`에 등록한 권한으로 여섯 원격을 직접 조회하므로 소스 checkout이 없는 새 호스트에서도 실행할 수 있습니다.

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
