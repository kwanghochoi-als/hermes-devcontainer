"""Exercise the observer credential with real Git configuration in a disposable home."""

from contextlib import redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "project-cli/git_auth.py"
run = subprocess.run


class ObserverGitAuth(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.home), "PATH": os.environ["PATH"], "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false",
        }, clear=True))
        spec = importlib.util.spec_from_file_location("observer_git_auth", SCRIPT)
        self.auth = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.auth)
        self.mirror = self.home / ".cache/pharmmate-observer/pharmmate.git"
        self.checkout = self.home / "other.git"
        for path in (self.mirror, self.checkout):
            run(["git", "init", "--bare", "-q", str(path)], check=True)
        run(["git", "config", "--global", "credential.helper",
             "!f() { echo username=desktop; echo password=desktop-fixture; }; f"], check=True)
        self.checked = []
        self.inaccessible = ""

    def github(self, args, **kwargs):
        if "ls-remote" not in args:
            return run(args, **kwargs)
        url, ref = args[-2:]
        self.assertEqual(ref, "refs/heads/main")
        self.assertNotIn("REMOTE_CONTAINERS_IPC", kwargs["env"])
        result = run(["git", "credential", "fill"], input=f"url={url}\n\n", text=True,
                     cwd=kwargs["cwd"], env=kwargs["env"], capture_output=True)
        self.checked.append(url)
        self.assertEqual(result.stdout.splitlines(), ["protocol=https", "host=github.com",
                         f"path={url.removeprefix('https://github.com/')}", "username=x-access-token",
                         "password=fixture-read"])
        rc = 128 if url.endswith("/" + self.inaccessible + ".git") else 0
        return subprocess.CompletedProcess(args, rc, b"0123456789\trefs/heads/main\n", b"")

    def setup_token(self):
        output = io.StringIO()
        with (patch("sys.stdin.isatty", return_value=True), patch("getpass.getpass", return_value="fixture-read"),
              patch.object(self.auth.subprocess, "run", side_effect=self.github), redirect_stdout(output)):
            self.auth.setup()
        self.assertNotIn("fixture-read", output.getvalue())

    def lookup(self, directory, url):
        return run(["git", "--git-dir", str(directory), "credential", "fill"], text=True,
                   input=f"url={url}\n\n", capture_output=True).stdout

    def test_scope_and_container_recreation(self):
        self.setup_token()
        self.assertEqual(self.checked, [f"https://github.com/alssolution/{r}.git" for r in self.auth.REPOS])
        self.assertEqual(stat.S_IMODE(self.auth.TOKEN.stat().st_mode), 0o600)
        self.assertNotIn("fixture-read", self.auth.CONFIG.read_text())
        url = "https://github.com/alssolution/pharmmate"
        self.assertIn("password=fixture-read", self.lookup(self.mirror, url))
        self.assertIn("password=desktop-fixture", self.lookup(self.checkout, url))
        for other in (url + "-other", url + "/nested", url.replace("alssolution", "elsewhere"),
                      url.replace("github.com", "example.org"), url.replace("https:", "http:")):
            self.assertNotIn("fixture-read", self.lookup(self.mirror, other))
        (self.home / ".gitconfig").unlink()
        self.auth.install_include()
        self.auth.install_include()
        self.assertEqual((self.home / ".gitconfig").read_text().count(str(self.auth.CONFIG)), 1)
        self.assertIn("password=fixture-read", self.lookup(self.mirror, url + ".git"))
        self.checked.clear()
        with patch.object(self.auth.subprocess, "run", side_effect=self.github), redirect_stdout(io.StringIO()):
            self.auth.status()
        self.assertEqual(len(self.checked), 6)

    def test_failed_rotation_preserves_existing_credential(self):
        self.setup_token()
        self.auth.TOKEN.write_text("previous-fixture\n")
        self.inaccessible = "jenkins-lib"
        with self.assertRaisesRegex(SystemExit, "jenkins-lib"):
            self.setup_token()
        self.assertEqual(self.auth.TOKEN.read_text(), "previous-fixture\n")
        self.assertEqual(set(self.auth.AUTH_DIR.iterdir()), {self.auth.TOKEN, self.auth.CONFIG})
