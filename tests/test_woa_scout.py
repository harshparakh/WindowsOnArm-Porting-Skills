import importlib.util
import pathlib
import unittest


MODULE_PATH = pathlib.Path(__file__).parents[1] / "scripts" / "woa_scout.py"
SPEC = importlib.util.spec_from_file_location("woa_scout", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class ScoutTests(unittest.TestCase):
    def test_parse_repository_accepts_slug_and_url(self):
        self.assertEqual(
            ("Flow-Launcher", "Flow.Launcher"),
            MODULE.parse_repository("Flow-Launcher/Flow.Launcher"),
        )
        self.assertEqual(
            ("Flow-Launcher", "Flow.Launcher"),
            MODULE.parse_repository("https://github.com/Flow-Launcher/Flow.Launcher.git"),
        )

    def test_assessment_detects_x64_release_gap_and_packaging_risk(self):
        metadata = {
            "owner": {"login": "example"},
            "name": "launcher",
            "full_name": "example/launcher",
            "html_url": "https://github.com/example/launcher",
            "default_branch": "main",
            "pushed_at": "2026-09-01T00:00:00Z",
            "license": {"spdx_id": "MIT"},
            "language": "C#",
            "stargazers_count": 5000,
            "forks_count": 100,
            "open_issues_count": 20,
        }
        releases = [
            {
                "tag_name": "v1.0.0",
                "published_at": "2026-08-01T00:00:00Z",
                "html_url": "https://github.com/example/launcher/releases/tag/v1.0.0",
                "draft": False,
                "prerelease": False,
                "assets": [
                    {
                        "name": "launcher-win-x64.zip",
                        "size": 10,
                        "download_count": 1000,
                        "browser_download_url": "https://example.invalid/x64",
                    }
                ],
            }
        ]
        paths = [
            "Launcher.sln",
            "Launcher/Launcher.csproj",
            "Launcher/Properties/PublishProfiles/x64.pubxml",
            "Scripts/squirrel.ps1",
            "Plugins/example.dll",
            "Tests/Launcher.Tests.csproj",
        ]
        contents = {
            "Launcher/Launcher.csproj": '<RuntimeIdentifier>win-x64</RuntimeIdentifier>',
            "Scripts/squirrel.ps1": "Squirrel --releasify",
        }
        report = MODULE.build_assessment(
            metadata,
            releases,
            paths,
            contents,
            [{"number": 1, "title": "ARM64", "state": "open"}],
            [],
            "test",
            [],
        )
        self.assertTrue(report["architecture"]["releaseAssetGap"])
        self.assertGreater(report["architecture"]["x64ReferenceCount"], 0)
        self.assertGreaterEqual(report["scores"]["technicalRisk"], 5)
        self.assertIn("portingReadiness", report["scores"])
        self.assertEqual("not-assessed", report["deliveryWindow"]["fit"])
        self.assertNotIn("oneWeekFeasible", report)
        self.assertIn("squirrel", {item["name"] for item in report["packaging"]})

    def test_delivery_window_is_optional_and_project_specific(self):
        effort = MODULE.estimate_effort(5)

        no_window = MODULE.assess_delivery_window(effort, None)
        hackathon_window = MODULE.assess_delivery_window(effort, 7)
        planned_window = MODULE.assess_delivery_window(effort, 30)

        self.assertEqual("not-assessed", no_window["fit"])
        self.assertEqual("conditional", hackathon_window["fit"])
        self.assertEqual("strong", planned_window["fit"])
        self.assertEqual(7, hackathon_window["days"])

    def test_high_risk_candidate_does_not_fit_short_window(self):
        effort = MODULE.estimate_effort(2)
        delivery_window = MODULE.assess_delivery_window(effort, 7)

        self.assertEqual("large", effort["band"])
        self.assertEqual("unlikely", delivery_window["fit"])

    def test_arm64_release_asset_closes_asset_gap(self):
        summary = MODULE.release_summary(
            [
                {
                    "tag_name": "v2",
                    "draft": False,
                    "prerelease": False,
                    "assets": [
                        {"name": "app-win-x64.zip", "download_count": 1},
                        {"name": "app-win-arm64.zip", "download_count": 1},
                    ],
                }
            ]
        )
        self.assertTrue(summary["arm64Assets"])
        self.assertTrue(summary["x64Assets"])
        self.assertTrue(summary["windowsAssets"])

    def test_root_level_tests_directory_is_detected(self):
        self.assertTrue(MODULE.detect_tests(["tests/App.Tests.csproj"]))
        self.assertTrue(MODULE.detect_tests(["App.Tests/App.Tests.csproj"]))
        self.assertFalse(MODULE.detect_tests(["src/App.csproj"]))


if __name__ == "__main__":
    unittest.main()
