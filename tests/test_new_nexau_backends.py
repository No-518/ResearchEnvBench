import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


HARNESS_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(HARNESS_ROOT / "m2"))
import run_one_job as m2  # noqa: E402

sys.path.insert(0, str(HARNESS_ROOT))
import host_orchestrator as orch  # noqa: E402


class TestNewNexauBackends(unittest.TestCase):
    def test_m2_normalize_backend_new_baselines(self) -> None:
        self.assertEqual(m2.normalize_backend("nexau_gemini30"), "nexau_gemini30")
        self.assertEqual(m2.normalize_backend("gemini30"), "nexau_gemini30")
        self.assertEqual(m2.normalize_backend("nexau_claude_sonnet45"), "nexau_claude_sonnet45")
        self.assertEqual(m2.normalize_backend("sonnet45"), "nexau_claude_sonnet45")
        self.assertEqual(m2.normalize_backend("nexau_minimax25"), "nexau_minimax25")
        self.assertEqual(m2.normalize_backend("m2.5"), "nexau_minimax25")
        self.assertEqual(m2.normalize_backend("deepseek3.1"), "nexau_deepseek31_nexn1")
        self.assertEqual(m2.normalize_backend("nexau_deepseek31_nexn1"), "nexau_deepseek31_nexn1")

    def test_m2_stdout_json_mode_for_nexau_variants(self) -> None:
        self.assertEqual(m2.default_stdout_json_mode("nexau_deepseek31_nexn1", "always"), "never")
        self.assertEqual(m2.default_stdout_json_mode("nexau_gemini30", "always"), "never")
        self.assertEqual(m2.default_stdout_json_mode("nexau_claude_sonnet45", "always"), "never")
        self.assertEqual(m2.default_stdout_json_mode("nexau_minimax25", "always"), "never")
        self.assertEqual(m2.default_stdout_json_mode("codex", "always"), "always")

    def test_host_normalize_baseline_new_baselines(self) -> None:
        self.assertEqual(orch.normalize_baseline("deepseek3.1"), "nexau_deepseek31_nexn1")
        self.assertEqual(orch.normalize_baseline("nexau_deepseek31_nexn1"), "nexau_deepseek31_nexn1")
        self.assertEqual(orch.normalize_baseline("nexau_gemini30"), "nexau_gemini30")
        self.assertEqual(orch.normalize_baseline("gemini-3.0"), "nexau_gemini30")
        self.assertEqual(orch.normalize_baseline("nexau-claude-sonnet-45"), "nexau_claude_sonnet45")
        self.assertEqual(orch.normalize_baseline("sonnet45"), "nexau_claude_sonnet45")
        self.assertEqual(orch.normalize_baseline("nexau_minimax25"), "nexau_minimax25")
        self.assertEqual(orch.normalize_baseline("minimax-m2.5"), "nexau_minimax25")

    def test_runners_json_has_new_backends(self) -> None:
        p = HARNESS_ROOT / "tools" / "env_setup_runner" / "runners.json"
        obj = json.loads(p.read_text(encoding="utf-8"))
        backends = obj.get("backends", {})
        placeholders = obj.get("placeholders", {})

        self.assertIn("codex_model", placeholders)
        self.assertIn("claude_model", placeholders)
        self.assertIn("nexau_generic_agent_config", placeholders)
        self.assertIn("nexau_deepseek31_agent_config", placeholders)

        self.assertIn("nexau_deepseek31_nexn1", backends)
        deepseek_cmd = " ".join(backends["nexau_deepseek31_nexn1"].get("argv", []))
        self.assertIn("NEXAU_AGENT_CONFIG", deepseek_cmd)
        self.assertIn("{nexau_deepseek31_agent_config}", deepseek_cmd)
        self.assertIn("LLM_API_TYPE", deepseek_cmd)
        self.assertIn("LLM_TOOL_CALL_MODE", deepseek_cmd)
        self.assertIn("${{LLM_API_TYPE:-anthropic_chat_completion}}", deepseek_cmd)
        self.assertIn("${{LLM_TOOL_CALL_MODE:-anthropic}}", deepseek_cmd)
        self.assertEqual((backends["nexau_deepseek31_nexn1"].get("report") or {}).get("mode"), "agent_writes_file")

        for b in ("nexau_gemini30", "nexau_claude_sonnet45", "nexau_minimax25"):
            self.assertIn(b, backends)
            cmd = " ".join(backends[b].get("argv", []))
            self.assertIn("NEXAU_AGENT_CONFIG", cmd)
            self.assertIn("{nexau_generic_agent_config}", cmd)
            self.assertIn("LLM_API_TYPE", cmd)
            self.assertIn("LLM_TOOL_CALL_MODE", cmd)
            self.assertIn("${{LLM_API_TYPE:-anthropic_chat_completion}}", cmd)
            self.assertIn("${{LLM_TOOL_CALL_MODE:-anthropic}}", cmd)
            self.assertEqual((backends[b].get("report") or {}).get("mode"), "agent_writes_file")

    def test_generic_nexau_config_has_required_env_placeholders(self) -> None:
        p = HARNESS_ROOT / "tools" / "env_setup_runner" / "nexau_configs" / "nexau_generic_llm.yaml"
        txt = p.read_text(encoding="utf-8")
        self.assertIn("${env.LLM_MODEL}", txt)
        self.assertIn("${env.LLM_BASE_URL}", txt)
        self.assertIn("${env.LLM_API_KEY}", txt)
        self.assertIn("${env.LLM_API_TYPE}", txt)
        self.assertIn("${env.LLM_TOOL_CALL_MODE}", txt)

    def test_runner_driver_has_deepseek31_config_variable(self) -> None:
        p = HARNESS_ROOT / "tools" / "env_setup_runner" / "run_env_setup_agent.py"
        txt = p.read_text(encoding="utf-8")
        self.assertIn("nexau_deepseek31_agent_config", txt)
        self.assertIn("NEXAU_DEEPSEEK31_AGENT_CONFIG", txt)
        self.assertIn("codex_model", txt)
        self.assertIn("CODEX_MODEL", txt)
        self.assertIn("claude_model", txt)
        self.assertIn("CLAUDE_MODEL", txt)

    def test_matrix_generator_outputs_full_and_smoke_files(self) -> None:
        script = HARNESS_ROOT / "tools" / "matrix" / "make_nexau_model_matrices.py"

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            src = td_path / "src.jsonl"
            out_dir = td_path / "out"
            rows = [
                {
                    "job_id": "a1",
                    "repo_full_name": "Auto1111SDK/Auto1111SDK",
                    "repo_url": "https://github.com/Auto1111SDK/Auto1111SDK",
                    "commit_sha": "abc",
                    "baseline": "nexau",
                    "hardware_bucket": "auto",
                },
                {
                    "job_id": "b2",
                    "repo_full_name": "LMCache/LMCache",
                    "repo_url": "https://github.com/LMCache/LMCache",
                    "commit_sha": "def",
                    "baseline": "nexau",
                    "hardware_bucket": "auto",
                },
            ]
            src.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

            subprocess.check_call(
                [
                    sys.executable,
                    str(script),
                    "--source",
                    str(src),
                    "--out-dir",
                    str(out_dir),
                    "--smoke-repo",
                    "Auto1111SDK/Auto1111SDK",
                ]
            )

            baselines = (
                "nexau_deepseek31_nexn1",
                "nexau_gemini30",
                "nexau_claude_sonnet45",
                "nexau_minimax25",
            )
            for b in baselines:
                full = out_dir / f"run_matrix_{b}.jsonl"
                smoke = out_dir / f"run_matrix_smoke_{b}.jsonl"
                self.assertTrue(full.exists())
                self.assertTrue(smoke.exists())

                full_rows = [json.loads(x) for x in full.read_text(encoding="utf-8").splitlines() if x.strip()]
                smoke_rows = [json.loads(x) for x in smoke.read_text(encoding="utf-8").splitlines() if x.strip()]
                self.assertEqual(len(full_rows), 2)
                self.assertEqual(len(smoke_rows), 1)
                self.assertTrue(all(r.get("baseline") == b for r in full_rows))
                self.assertTrue(all(r.get("baseline") == b for r in smoke_rows))
                self.assertEqual(smoke_rows[0].get("repo_full_name"), "Auto1111SDK/Auto1111SDK")


if __name__ == "__main__":
    unittest.main()
