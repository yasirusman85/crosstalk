"""End-to-end tests: simulate parallel Claude Code sessions by feeding hook JSON."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "crosstalk.py"


class Crosstalk(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self.tmp.name) / "proj"
        self.proj.mkdir()
        self.env = {**os.environ, "CROSSTALK_HOME": str(Path(self.tmp.name) / "home")}
        self.env.pop("CROSSTALK_SESSION", None)
        self.env.pop("CROSSTALK_PROJECT", None)

    def tearDown(self):
        self.tmp.cleanup()

    def hook(self, event, sid, extra=None, env=None):
        data = {"session_id": sid, "cwd": str(self.proj), **(extra or {})}
        out = subprocess.run(
            [sys.executable, str(SCRIPT), "hook", event],
            input=json.dumps(data), capture_output=True, text=True,
            env={**self.env, **(env or {})}, timeout=20,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stderr, "")
        return json.loads(out.stdout) if out.stdout.strip() else None

    def cli(self, *args, env=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
            cwd=self.proj, env={**self.env, **(env or {})}, timeout=20,
        )

    def edit(self, sid, path):
        payload = {"tool_name": "Edit", "tool_input": {"file_path": str(self.proj / path)}}
        pre = self.hook("pre-edit", sid, payload)
        self.hook("post-edit", sid, payload)
        return pre

    def test_alone_is_silent(self):
        self.assertIsNone(self.hook("session-start", "aaaa1111"))
        self.assertIsNone(self.hook("prompt", "aaaa1111", {"prompt": "fix the login bug"}))
        self.assertIsNone(self.edit("aaaa1111", "src/auth.py"))

    def test_two_sessions_see_each_other(self):
        self.hook("session-start", "aaaa1111")
        self.hook("prompt", "aaaa1111", {"prompt": "fix the login bug"})
        self.edit("aaaa1111", "src/auth.py")

        start = self.hook("session-start", "bbbb2222")
        ctx = start["hookSpecificOutput"]["additionalContext"]
        self.assertIn("fix the login bug", ctx)
        self.assertIn("src/auth.py", ctx)
        self.assertIn("1 other live session", start["systemMessage"])

        # A sees B's goal on its next turn, and only once.
        self.hook("prompt", "bbbb2222", {"prompt": "add dark mode"})
        news = self.hook("prompt", "aaaa1111", {"prompt": "continue"})
        self.assertIn("add dark mode", news["hookSpecificOutput"]["additionalContext"])
        again = self.hook("prompt", "aaaa1111", {"prompt": "continue"})
        self.assertIsNone(again)

    def test_overlap_warning_and_modes(self):
        self.hook("session-start", "aaaa1111")
        self.hook("prompt", "aaaa1111", {"prompt": "refactor auth"})
        self.edit("aaaa1111", "src/auth.py")
        self.hook("session-start", "bbbb2222")

        warn = self.edit("bbbb2222", "src/auth.py")
        self.assertIn("src/auth.py", warn["systemMessage"])
        self.assertIn("refactor auth", warn["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("permissionDecision", warn["hookSpecificOutput"])

        payload = {"tool_name": "Write", "tool_input": {"file_path": str(self.proj / "src/auth.py")}}
        ask = self.hook("pre-edit", "bbbb2222", payload, env={"CROSSTALK_MODE": "ask"})
        self.assertEqual(ask["hookSpecificOutput"]["permissionDecision"], "ask")
        block = self.hook("pre-edit", "bbbb2222", payload, env={"CROSSTALK_MODE": "block"})
        self.assertEqual(block["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIsNone(self.hook("pre-edit", "bbbb2222", payload, env={"CROSSTALK_MODE": "off"}))

        # Untouched file: no warning. Re-editing your own file: no warning.
        self.assertIsNone(self.edit("bbbb2222", "src/ui.py"))
        self.assertIsNone(self.edit("bbbb2222", "src/ui.py"))
        # ...but now B has edited auth.py too, so A gets warned about B.
        self.assertIn("src/auth.py", self.edit("aaaa1111", "src/auth.py")["systemMessage"])

    def test_ended_session_stops_warning(self):
        self.hook("session-start", "aaaa1111")
        self.edit("aaaa1111", "src/auth.py")
        self.hook("session-end", "aaaa1111")
        self.hook("session-start", "bbbb2222")
        self.assertIsNone(self.edit("bbbb2222", "src/auth.py"))

    def test_notes_broadcast(self):
        envfile = Path(self.tmp.name) / "env.sh"
        envfile.touch()
        self.hook("session-start", "aaaa1111", env={"CLAUDE_ENV_FILE": str(envfile)})
        self.assertIn("CROSSTALK_SESSION=aaaa1111", envfile.read_text())
        self.hook("session-start", "bbbb2222")

        out = self.cli("note", "renamed getUser to fetchUser", env={"CROSSTALK_SESSION": "aaaa1111"})
        self.assertEqual(out.returncode, 0, out.stderr)
        news = self.hook("prompt", "bbbb2222", {"prompt": "next"})
        self.assertIn("NOTE: renamed getUser to fetchUser",
                      news["hookSpecificOutput"]["additionalContext"])

        # New sessions get recent notes even if nobody else is live.
        self.hook("session-end", "aaaa1111")
        self.hook("session-end", "bbbb2222")
        late = self.hook("session-start", "cccc3333")
        self.assertIn("fetchUser", late["hookSpecificOutput"]["additionalContext"])

        status = self.cli("status")
        self.assertIn("fetchUser", status.stdout)

    def test_concurrent_writers(self):
        sids = [f"s{i:03d}xxxx" for i in range(8)]
        for s in sids:
            self.hook("session-start", s)

        def work(s):
            for n in range(5):
                self.edit(s, f"f{n}.py")

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(work, sids))
        out = self.cli("log", "1000")
        self.assertEqual(out.stdout.count(" edited "), 8 * 5)

    def test_bad_input_never_breaks_session(self):
        out = subprocess.run([sys.executable, str(SCRIPT), "hook", "prompt"],
                             input="not json", capture_output=True, text=True, env=self.env)
        self.assertEqual(out.returncode, 0)


if __name__ == "__main__":
    unittest.main()
