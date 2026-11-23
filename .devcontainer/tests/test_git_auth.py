"""Check that Git receives only the selected project's HTTPS credential."""
import importlib.util
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "project-cli/git_auth.py"
spec = importlib.util.spec_from_file_location("observer_git_auth", SCRIPT)
auth = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auth)


class ObserverGitAuth(unittest.TestCase):
    def test_scope_and_rotation(self):
        source = "https://git.example.com/dev/api.git"
        sources = {"api": source}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p_01234567/token"
            with patch("sys.stdin.isatty", return_value=True), patch("getpass.getpass", return_value="fixture"), patch.object(auth, "verify") as verify:
                auth.configure_token("setup", sources, path)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                verify.side_effect = ValueError("remote offline")
                with self.assertRaisesRegex(ValueError, "offline"), patch("getpass.getpass", return_value="replacement"):
                    auth.configure_token("setup", sources, path)
            self.assertEqual(path.read_text().strip(), "fixture")

            def lookup(url, policy=sources, token=path):
                return subprocess.run(["git", "credential", "fill"], cwd=tmp, env=auth.git_env(policy, token),
                                      input=f"url={url}\n\n", capture_output=True, text=True).stdout
            self.assertIn("password=fixture", lookup(source))
            for url in (source + "2", source.replace("dev/", "other/"), source.replace("https:", "http:"),
                        source.replace("example.com", "example.org"), source + "/nested"):
                self.assertNotIn("fixture", lookup(url))
            self.assertNotIn("fixture", lookup(source, token=Path(tmp) / "p_other/token"))
            self.assertNotIn("fixture", lookup(source, {**sources, "other": "https://other.host/b.git"}))
            same_host = "https://GIT.example.com:443/dev/web.git"
            other_host = "https://other.host/b.git"
            allowed = auth.credential_sources({"local": "/work/review", **sources, "web": same_host, "other": other_host})
            self.assertEqual(allowed, {**sources, "web": same_host})
            self.assertIn("password=fixture", lookup(source, allowed))
            self.assertIn("password=fixture", lookup("https://git.example.com/dev/web.git", allowed))
            self.assertNotIn("fixture", lookup(other_host, allowed))
