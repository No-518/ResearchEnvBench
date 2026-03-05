import json
import subprocess
import sys
import unittest
from pathlib import Path


HARNESS_ROOT = Path(__file__).resolve().parents[1]


class TestPaperBaselineModelFlags(unittest.TestCase):
    def test_run_one_job_help_has_model_flags(self) -> None:
        cmd = [sys.executable, "-m", "m2.run_one_job", "--help"]
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            cwd=str(HARNESS_ROOT),
        )
        self.assertEqual(p.returncode, 0)
        self.assertIn("--claude-model", p.stdout)
        self.assertIn("--codex-model", p.stdout)

    def test_host_orchestrator_help_has_model_flags(self) -> None:
        cmd = [sys.executable, str(HARNESS_ROOT / "host_orchestrator.py"), "--help"]
        p = subprocess.run(cmd, capture_output=True, text=True, check=False)
        self.assertEqual(p.returncode, 0)
        self.assertIn("--claude-model", p.stdout)
        self.assertIn("--codex-model", p.stdout)

    def test_runner_backends_use_model_placeholders(self) -> None:
        p = HARNESS_ROOT / "tools" / "env_setup_runner" / "runners.json"
        obj = json.loads(p.read_text(encoding="utf-8"))
        backends = obj.get("backends", {})

        codex_cmd = " ".join((backends.get("codex") or {}).get("argv", []))
        self.assertIn("{codex_model}", codex_cmd)
        self.assertIn("model_args", codex_cmd)
        self.assertIn("--model", codex_cmd)

        claude_cmd = " ".join((backends.get("claude_code") or {}).get("argv", []))
        self.assertIn("{claude_model}", claude_cmd)


if __name__ == "__main__":
    unittest.main()
