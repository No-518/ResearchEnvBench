import unittest
from pathlib import Path


HARNESS_ROOT = Path(__file__).resolve().parents[1]


class TestReleaseDockerfileOfficialBase(unittest.TestCase):
    def test_uses_official_cuda_base_image(self) -> None:
        p = HARNESS_ROOT / "dockerfiles" / "ultimate.Dockerfile"
        txt = p.read_text(encoding="utf-8")
        self.assertIn("FROM nvidia/cuda:12.4.1-devel-ubuntu22.04", txt)

    def test_does_not_use_non_official_mirror_domain(self) -> None:
        p = HARNESS_ROOT / "dockerfiles" / "ultimate.Dockerfile"
        txt = p.read_text(encoding="utf-8").lower()
        self.assertNotIn("daocloud", txt)


if __name__ == "__main__":
    unittest.main()
