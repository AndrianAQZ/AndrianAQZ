from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import generate_profile  # noqa: E402


class GenerateProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = json.loads((ROOT / "profile.config.json").read_text(encoding="utf-8"))
        cls.template = (ROOT / "README.template.md").read_text(encoding="utf-8")
        cls.data = json.loads(
            (ROOT / "tests/fixtures/github-data.json").read_text(encoding="utf-8")
        )
        cls.rendered = generate_profile.render_profile(cls.template, cls.config, cls.data)

    def test_featured_original_project_is_rendered(self) -> None:
        self.assertIn("[OpenCodex](https://github.com/AndrianAQZ/OpenCodex)", self.rendered)
        self.assertIn("Safety-first UX", self.rendered)

    def test_private_and_imported_projects_are_not_rendered(self) -> None:
        self.assertNotIn("private-secret", self.rendered)
        self.assertNotIn("Must never appear", self.rendered)
        self.assertNotIn("Imported project", self.rendered)

    def test_fork_is_activity_not_featured_work(self) -> None:
        self.assertIn("Pushed 2 commits to", self.rendered)
        self.assertIn("AndrianAQZ/hermes-agent", self.rendered)
        featured = self.rendered.split("## Featured work", 1)[1].split(
            "## Recent public activity", 1
        )[0]
        self.assertNotIn("hermes-agent", featured)

    def test_profile_refresh_and_excluded_activity_are_hidden(self) -> None:
        activity = self.rendered.split("## Recent public activity", 1)[1]
        self.assertNotIn("profile123", activity)
        self.assertNotIn("ldsc123", activity)
        self.assertNotIn("AndrianAQZ/AndrianAQZ", activity)

    def test_repository_updates_are_used_when_events_are_not_meaningful(self) -> None:
        data = dict(self.data)
        data["events"] = []
        rendered = generate_profile.render_profile(self.template, self.config, data)
        activity = rendered.split("## Recent public activity", 1)[1]
        self.assertIn("Updated public fork", activity)
        self.assertIn("AndrianAQZ/hermes-agent", activity)
        self.assertIn("Updated public project", activity)
        self.assertIn("AndrianAQZ/OpenCodex", activity)
        self.assertNotIn("AndrianAQZ/ldsc", activity)
        self.assertNotIn("private-secret", activity)

    def test_all_template_markers_remain_well_formed(self) -> None:
        for name in ["INTRO", "ABOUT", "TOOLBOX", "FEATURED", "ACTIVITY", "STATUS"]:
            self.assertEqual(self.rendered.count(f"AUTO:{name}:START"), 1)
            self.assertEqual(self.rendered.count(f"AUTO:{name}:END"), 1)

    def test_status_includes_low_noise_heartbeat(self) -> None:
        self.assertIn("Automation checked:", self.rendered)
        self.assertIn("monthly heartbeat", self.rendered)


if __name__ == "__main__":
    unittest.main()
