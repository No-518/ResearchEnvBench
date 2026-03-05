import json
import unittest
from pathlib import Path


HARNESS_ROOT = Path(__file__).resolve().parents[1]


class TestCodexEnvCompatMapping(unittest.TestCase):
    def test_codex_backend_supports_legacy_env_aliases(self) -> None:
        p = HARNESS_ROOT / "tools" / "env_setup_runner" / "runners.json"
        obj = json.loads(p.read_text(encoding="utf-8"))
        cmd = " ".join((obj.get("backends", {}).get("codex", {}).get("argv", [])))

        self.assertIn("CODEX_BASE_URL", cmd)
        self.assertIn("CODEX_API_KEY", cmd)
        self.assertIn("CODEX_TOKEN", cmd)
        self.assertIn("OPENAI_BASE_URL", cmd)
        self.assertIn("OPENAI_API_KEY", cmd)

        self.assertIn("if [ -z \"${{OPENAI_BASE_URL:-}}\" ] && [ -n \"${{CODEX_BASE_URL:-}}\" ]", cmd)
        self.assertIn("if [ -z \"${{OPENAI_API_KEY:-}}\" ] && [ -n \"${{CODEX_API_KEY:-}}\" ]", cmd)
        self.assertIn("if [ -z \"${{OPENAI_API_KEY:-}}\" ] && [ -n \"${{CODEX_TOKEN:-}}\" ]", cmd)
        self.assertIn("OPENAI_API_KEY is not set", cmd)


if __name__ == "__main__":
    unittest.main()
