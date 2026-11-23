"""Project-scoped HTTPS credentials used only by the controller's Git process."""
import getpass
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit


def token_path(project_id):
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "git-auth" / project_id / "token"


def https_host(authority):
    return authority.lower().removesuffix(":443")


def credential_sources(sources):
    """Discovery appends sources. The project's first HTTPS host retains its token scope."""
    urls = {k: v for k, v in sources.items() if v.startswith("https://")}
    if not urls:
        return {}
    host = https_host(urlsplit(next(iter(urls.values()))).netloc)
    return {k: v for k, v in urls.items() if https_host(urlsplit(v).netloc) == host}


def git_env(sources, token):
    env = {k: os.environ[k] for k in ("PATH", "HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy",
                                       "SSL_CERT_FILE", "GIT_SSL_CAINFO") if k in os.environ}
    urls = [url for url in sources.values() if url.startswith("https://")]
    helper = shlex.join(["/usr/bin/python3", "-I", "-B", str(Path(__file__).resolve())])
    env.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0",
               GIT_ASKPASS="/bin/false", GIT_ALLOW_PROTOCOL="https:file", OBSERVER_TOKEN_FILE=str(token),
               OBSERVER_GIT_URLS=json.dumps(urls), GIT_CONFIG_COUNT="3", GIT_CONFIG_KEY_0="credential.helper",
               GIT_CONFIG_VALUE_0=helper, GIT_CONFIG_KEY_1="credential.useHttpPath", GIT_CONFIG_VALUE_1="true",
               GIT_CONFIG_KEY_2="core.hooksPath", GIT_CONFIG_VALUE_2="/dev/null")
    return env


def credential_get():
    fields = dict(line.rstrip("\n").split("=", 1) for line in sys.stdin if "=" in line)
    urls = [urlsplit(url) for url in json.loads(os.environ.get("OBSERVER_GIT_URLS", "[]"))]
    host = https_host(fields.get("host", ""))
    if (fields.get("protocol") != "https" or {https_host(u.netloc) for u in urls} != {host}
            or not any(u.scheme == "https" and u.path == "/" + fields.get("path", "") for u in urls)):
        return
    token = Path(os.environ["OBSERVER_TOKEN_FILE"])
    if token.is_file():
        print("username=x-access-token\npassword=" + token.read_text().strip())


def verify(sources, token):
    env = git_env(credential_sources(sources), token)
    for name, url in sources.items():
        if url.startswith("https://"):
            try:
                result = subprocess.run(["git", "-c", "http.followRedirects=false", "ls-remote", "--exit-code", "--", url,
                                     "refs/heads/main"], cwd="/", env=env, capture_output=True,
                                    stdin=subprocess.DEVNULL, timeout=45)
            except (subprocess.TimeoutExpired, OSError) as exc:
                raise ValueError(f"{name} · Git 실행 또는 원격 연결을 확인하세요.") from exc
            if result.returncode or not result.stdout.strip():
                raise ValueError(f"{name} · 원격 main 읽기 권한과 네트워크를 확인하세요.")
            print(f"{name} · main 확인")


def configure(operation, selected):
    from hermes_cli import projects_db as pdb
    from .board import project_for_checkout
    with pdb.connect_closing() as conn:
        project = pdb.get_project(conn, selected) if selected else project_for_checkout(Path.cwd().resolve())
    if not project or not project.readonly:
        raise ValueError("읽기 전용 프로젝트 폴더에서 실행하거나 --project로 해당 프로젝트를 지정하세요.")
    path = token_path(project.id)
    sources = json.loads(project.observer_sources)
    configure_token(operation, sources, path)


def configure_token(operation, sources, path):
    if operation == "status":
        print("Git 읽기 토큰 ·", "등록됨" if path.is_file() else "미등록")
        if any(url.startswith("https://") for url in sources.values()):
            verify(sources, path)
        return
    allowed = credential_sources(sources)
    if not allowed:
        raise ValueError("토큰을 등록할 HTTPS 원격 저장소가 필요합니다.")
    if not sys.stdin.isatty():
        raise ValueError("로컬 대화형 터미널에서 실행하세요.")
    host = urlsplit(next(iter(allowed.values()))).netloc
    token = getpass.getpass(f"{host} 저장소 읽기 토큰 (입력 숨김) · ").strip()
    if not token or not token.isascii() or not token.isprintable() or any(c.isspace() for c in token):
        raise ValueError("유효한 읽기 토큰이 필요합니다.")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=path.parent) as temp:
        candidate = Path(temp) / "token"
        candidate.touch(mode=0o600)
        candidate.write_text(token + "\n")
        verify(allowed, candidate)
        candidate.replace(path)


if __name__ == "__main__" and sys.argv[1:] == ["get"]:
    credential_get()
