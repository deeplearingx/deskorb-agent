import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class FixtureIntegrityTests(unittest.TestCase):
    def test_repeatable_dataset_fixtures_exist_and_are_local(self):
        dataset = json.loads((Path(__file__).parent / "e2e_task_dataset.json").read_text(encoding="utf-8"))
        required = {
            "mock_store": Path("tests/fixtures/web/mock_store.html"),
            "mock_search": Path("tests/fixtures/web/mock_search.html"),
            "broken_python_project": Path("tests/fixtures/broken_python_project"),
            "network_failure": Path("tests/fixtures/network_failure"),
            "desktop_sandbox": Path("tests/fixtures/desktop_sandbox"),
            "window_probe": Path("tests/fixtures/window_probe"),
            "captcha_search": Path("tests/fixtures/captcha_search"),
        }
        for task in dataset["tasks"]:
            if task.get("tier") != "repeatable":
                continue
            fixture = str((task.get("setup") or {}).get("fixture") or "")
            if fixture not in required:
                continue
            path = ROOT / required[fixture]
            self.assertTrue(path.exists(), fixture)
            self.assertTrue(_is_relative_to(path.resolve(), ROOT.resolve()), fixture)

    def test_sensitive_fixture_values_are_redacted(self):
        text = (ROOT / "tests/fixtures/network_failure/connection.json").read_text(encoding="utf-8")
        self.assertIn("<redacted>", text)
        self.assertNotIn("sk-", text)

    def test_expansion_manifest_points_to_the_same_local_fixture_contract(self):
        expansion = json.loads((Path(__file__).parent / "e2e_task_dataset_expansions.json").read_text(encoding="utf-8"))
        self.assertEqual(len(expansion["cases"]), 49)
        for case in expansion["cases"]:
            fixture = str((case.get("setup") or {}).get("fixture") or "")
            self.assertNotIn("http://", fixture)
            self.assertNotIn("https://", fixture)


if __name__ == "__main__":
    unittest.main()
