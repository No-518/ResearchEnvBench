import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


HARNESS_ROOT = Path(__file__).resolve().parents[1]

# m2 is not a package; import by path.
sys.path.insert(0, str(HARNESS_ROOT / "m2"))
import run_one_job as m2  # noqa: E402

from m5 import build_master_table as m5  # noqa: E402


class TestDenominators(unittest.TestCase):
    def test_c0_reference_total_map_is_complete(self) -> None:
        self.assertEqual(len(m5.C0_REPO_BASELINE_TOTALS), 44)
        self.assertEqual(sum(m5.C0_REPO_BASELINE_TOTALS.values()), 2858)

    def test_categories_counts(self) -> None:
        p = HARNESS_ROOT / "scripts_repos_test_categories.csv"
        with p.open("r", encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))

        self.assertEqual(len(rows), 44)

        cpu_yes = sum(1 for r in rows if (r.get("supports_cpu") or "").strip() == "yes")
        single_yes = sum(1 for r in rows if (r.get("supports_single_gpu") or "").strip() == "yes")
        multi_yes = sum(1 for r in rows if (r.get("supports_multi_gpu") or "").strip() == "yes")

        self.assertEqual(cpu_yes, 29)
        self.assertEqual(single_yes, 43)
        self.assertEqual(multi_yes, 32)

    def test_m2_categories_skip_override_writes_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host_job_dir = Path(td)
            build_output = host_job_dir / "benchmark" / "build_output"
            (build_output / "cpu").mkdir(parents=True)

            info = m2._apply_categories_skip_overrides(
                host_job_dir=str(host_job_dir),
                script_id="NVlabs@Sana",
                repo_url="https://github.com/NVlabs/Sana",
                harness_dir=str(HARNESS_ROOT),
                decision_reason="test",
                only_if_failure_category=None,
            )
            self.assertIn("cpu", info.get("overrides_applied", []))

            res = json.loads((build_output / "cpu" / "results.json").read_text(encoding="utf-8"))
            self.assertEqual(res.get("status"), "skipped")

    def test_m2_categories_skip_override_overwrites_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            host_job_dir = Path(td)
            build_output = host_job_dir / "benchmark" / "build_output"
            stage_dir = build_output / "cpu"
            stage_dir.mkdir(parents=True)
            (stage_dir / "results.json").write_text(
                json.dumps(
                    {
                        "status": "failure",
                        "failure_category": "unknown",
                        "skip_reason": "unknown",
                        "stage": "cpu",
                        "exit_code": 1,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            info = m2._apply_categories_skip_overrides(
                host_job_dir=str(host_job_dir),
                script_id="NVlabs@Sana",
                repo_url="https://github.com/NVlabs/Sana",
                harness_dir=str(HARNESS_ROOT),
                decision_reason="test",
                only_if_failure_category=None,
            )
            self.assertIn("cpu", info.get("overrides_applied", []))

            res = json.loads((stage_dir / "results.json").read_text(encoding="utf-8"))
            self.assertEqual(res.get("status"), "skipped")

    def test_m5_summary_uses_fixed_denoms(self) -> None:
        rows = [
            {
                "run_id": "r",
                "baseline": "b",
                "repo_full_name": "Auto1111SDK/Auto1111SDK",
                "supports_cpu": "yes",
                "supports_single_gpu": "yes",
                "supports_multi_gpu": "yes",
                "c0_missing_imports": 1,
                "c0_total_imports": 999,  # ignored: canonical map has 213
                "c0_total_imports_baseline": "",
                "c1_cpu_status": "success",
                "c2_cuda_status": "failure",
                "c3_single_gpu_status": "success",
                "c4_multi_gpu_status": "failure",
                "c5_path_hallucinations_count": 0,
                "c5_version_hallucinations_count": 0,
                "c5_capability_hallucinations_count": 0,
                "agent_wall_time_sec": "",
                "env_prefix_size_mb": "",
                "job_dir": "",
            },
            {
                "run_id": "r",
                "baseline": "b",
                "repo_full_name": "kyegomez/VisionMamba",
                "supports_cpu": "no",
                "supports_single_gpu": "yes",
                "supports_multi_gpu": "no",
                "c0_missing_imports": 2,
                "c0_total_imports": 888,  # ignored: canonical map has 4
                "c0_total_imports_baseline": "",
                "c1_cpu_status": "success",  # should be ignored (not applicable)
                "c2_cuda_status": "success",
                "c3_single_gpu_status": "failure",
                "c4_multi_gpu_status": "success",  # should be ignored (not applicable)
                "c5_path_hallucinations_count": 0,
                "c5_version_hallucinations_count": 0,
                "c5_capability_hallucinations_count": 0,
                "agent_wall_time_sec": "",
                "env_prefix_size_mb": "",
                "job_dir": "",
            },
        ]

        summary = m5.build_summary_rows(rows)
        self.assertEqual(len(summary), 1)
        s = summary[0]

        self.assertEqual(s.get("job_count"), 2)
        self.assertEqual(s.get("c1_cpu_denom"), 1)
        self.assertEqual(s.get("c3_single_gpu_denom"), 2)
        self.assertEqual(s.get("c4_multi_gpu_denom"), 1)
        self.assertEqual(s.get("c2_cuda_denom"), 2)

        self.assertEqual(s.get("c1_cpu_success_over_all"), "1/1")
        self.assertEqual(s.get("c3_single_gpu_success_over_all"), "1/2")
        self.assertEqual(s.get("c4_multi_gpu_success_over_all"), "0/1")

        # C0 total should prefer canonical repo totals (213+4), not reported totals (999+888).
        self.assertEqual(s.get("c0_total_sum"), 217)
        self.assertEqual(s.get("c0_total_sum_reported"), 1887)
        self.assertEqual(s.get("c0_missing_over_total"), "3/217")


if __name__ == "__main__":
    unittest.main()
