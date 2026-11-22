"""GitHub read credentials for the Pharmmate observer's bare mirrors."""

import getpass
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile


OWNER = "alssolution"
REPOS = ("pharmmate", "pharmmate-als", "gitops-als", "infra-als", "jenkins-als", "jenkins-lib")
TOKEN_PATH = Path(".hermes/git-auth/pharmmate.token")
TOKEN = Path.home() / TOKEN_PATH
AUTH_DIR = TOKEN.parent
CONFIG = AUTH_DIR / "pharmmate.gitconfig"
INCLUDE = "includeIf.gitdir:~/.cache/pharmmate-observer/.path"


def has_include():
    return subprocess.run(["git", "config", "--global", "--fixed-value", "--get-all", INCLUDE, str(CONFIG)],
                          stdout=subprocess.DEVNULL).returncode == 0


def install_include():
    # Git ignores the included file until setup has verified and published it.
    if not has_include():
        subprocess.run(["git", "config", "--global", "--fixed-value", "--replace-all",
                        INCLUDE, str(CONFIG), str(CONFIG)], check=True)


def credential_get():
    fields = {}
    for line in sys.stdin:
        key, sep, value = line.rstrip("\n").partition("=")
        if not sep:
            break
        fields[key] = value
    owner, _, repo = fields.get("path", "").removesuffix(".git").partition("/")
    if (fields.get("protocol") != "https" or fields.get("host") != "github.com"
            or owner != OWNER or repo not in REPOS):
        return
    try:
        # The read-only job credential lives outside the persistent worker home.
        source = (Path("/run/hermes-job/pharmmate.token")
                  if os.environ.get("GIT_CONFIG_GLOBAL") == "/run/hermes-job/gitconfig" else TOKEN)
        token = source.read_text().strip()
    except FileNotFoundError:
        return
    if token:
        print(f"username=x-access-token\npassword={token}")


def verify(config, home):
    # Use only the supplied credential, even when setup runs inside an authenticated VS Code shell.
    env = {k: os.environ[k] for k in ("PATH", "HTTPS_PROXY", "https_proxy", "NO_PROXY",
                                      "no_proxy", "SSL_CERT_FILE", "GIT_SSL_CAINFO") if k in os.environ}
    env.update(HOME=str(home), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=str(config),
               GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="/bin/false")
    args = ["/usr/bin/git", "-c", "http.followRedirects=false", "ls-remote", "--exit-code"]
    for repo in REPOS:
        try:
            result = subprocess.run([*args, f"https://github.com/{OWNER}/{repo}.git", "refs/heads/main"],
                                    cwd="/", env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=20)
        except subprocess.TimeoutExpired:
            raise SystemExit(f"{OWNER}/{repo} · GitHub 응답 시간 초과") from None
        if result.returncode or not result.stdout.strip():
            raise SystemExit(f"{OWNER}/{repo} · 원격 main 조회 실패. 저장소 선택, Contents 읽기 권한, "
                             "조직 승인 상태와 네트워크를 확인하세요.")
        print(f"{OWNER}/{repo} · main 확인", flush=True)


def setup():
    if not sys.stdin.isatty():
        raise SystemExit("devcontainer의 대화형 터미널에서 실행하세요.")
    print(f"GitHub 소유자 {OWNER} · 대상 저장소 {', '.join(REPOS)}")
    token = getpass.getpass("Contents 읽기 권한을 가진 fine-grained PAT (입력 숨김) · ").strip()
    if not token or not token.isascii() or not token.isprintable() or any(c.isspace() for c in token):
        raise SystemExit("공백 없는 GitHub 토큰을 입력하세요.")
    AUTH_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=AUTH_DIR) as directory:
        staging_home = Path(directory)
        candidate = staging_home / TOKEN_PATH
        candidate.parent.mkdir(parents=True)
        with open(candidate, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as file:
            file.write(token + "\n")
        fragment = staging_home / "gitconfig"
        helper = shlex.join(["/usr/bin/python3", "-I", "-B", str(Path(__file__).resolve())])
        for key, value in (("useHttpPath", "true"), ("helper", ""), ("helper", helper)):
            subprocess.run(["git", "config", "--file", str(fragment), "--add",
                            f"credential.https://github.com.{key}", value], check=True)
        verify(fragment, staging_home)
        install_include()
        os.replace(candidate, TOKEN)
        os.replace(fragment, CONFIG)
    print("Hermes Git 읽기 인증을 등록했습니다. 다음 관찰 요청부터 적용됩니다.")


def status():
    if not TOKEN.is_file() or not CONFIG.is_file():
        print("Hermes Git 읽기 인증 · 미등록")
        return
    if not has_include():
        raise SystemExit("Git 설정 연결을 복구하려면 python3 /workspaces/.devcontainer/project-cli/git_auth.py install 을 실행하세요.")
    verify(CONFIG, Path.home())


if __name__ == "__main__":
    if sys.argv[1:] == ["install"]:
        install_include()
    elif sys.argv[1:] == ["get"]:
        credential_get()
