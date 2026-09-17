import argparse
import importlib.util
import io
import json
import pathlib
import unittest
from unittest import mock


MODULE_PATH = pathlib.Path(__file__).parents[1] / "scripts" / "woa_scout.py"
SPEC = importlib.util.spec_from_file_location("woa_scout", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class ScoutTests(unittest.TestCase):
    def release(self, names, tag="v1", prerelease=False, draft=False):
        return {
            "tag_name": tag,
            "prerelease": prerelease,
            "draft": draft,
            "published_at": "2026-09-01T00:00:00Z",
            "html_url": f"https://github.com/example/app/releases/tag/{tag}",
            "assets": [{"name": name, "download_count": 1} for name in names],
        }

    def assessment(self, releases, **kwargs):
        return MODULE.build_assessment(
            {"full_name": "example/app", "language": "C#"},
            releases,
            ["App.csproj", "tests/App.Tests.csproj"],
            {},
            [],
            [],
            "test",
            [],
            **kwargs,
        )

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
        self.assertEqual(["app-win-arm64.zip"], summary["arm64Assets"])
        self.assertEqual(["app-win-x64.zip"], summary["x64Assets"])
        self.assertEqual(["app-win-x64.zip", "app-win-arm64.zip"], summary["windowsAssets"])

    def test_windows_arm_binaries_close_selected_release_gap(self):
        names = [
            "app-win-arm64.zip",
            "app-windows-aarch64.7z",
            "app-ARM64.exe",
            "app-aarch64.msi",
            "app-win-arm64.msix",
            "app-win-arm64.appx",
            "app-win-arm64.msixbundle",
        ]
        report = self.assessment([self.release(names)])
        summary = report["release"]
        self.assertEqual(len(names), len(summary["latest"]["assets"]))
        self.assertEqual(names, summary["windowsArm64Assets"])
        self.assertEqual(names, summary["windowsAssets"])
        for index, name in enumerate(names):
            asset = summary["latest"]["assets"][index]
            self.assertEqual(name, asset["name"])
            self.assertEqual("windows", asset["platform"])
            self.assertEqual("arm64", asset["architecture"])
            self.assertEqual("binary", asset["kind"])
        self.assertFalse(report["architecture"]["releaseAssetGap"])
        self.assertTrue(report["architecture"]["latestReleaseHasArm64Asset"])
        self.assertEqual("windows-arm64-observed", report["architecture"]["releaseAssetGapStatus"])
        self.assertEqual("observed", report["architecture"]["windowsArm64Availability"])
        self.assertEqual(1, len(report["architecture"]["windowsArm64Evidence"]))
        self.assertEqual(names, report["architecture"]["windowsArm64Evidence"][0]["assets"])

    def test_other_platform_arm_assets_do_not_close_windows_gap(self):
        names = ["app-macos-arm64.dmg", "app-darwin-arm64.zip", "app-linux-aarch64.tar.gz"]
        report = self.assessment([self.release(["app-win-x64.zip", *names])])
        summary = report["release"]
        self.assertEqual(4, len(summary["latest"]["assets"]))
        self.assertEqual(names, summary["arm64Assets"])
        self.assertEqual(["app-win-x64.zip"], summary["windowsAssets"])
        self.assertEqual(["app-win-x64.zip"], summary["windowsX64Assets"])
        self.assertEqual([], summary["windowsArm64Assets"])
        for index, (name, platform) in enumerate(zip(names, ["macos", "macos", "linux"]), 1):
            asset = summary["latest"]["assets"][index]
            self.assertEqual(name, asset["name"])
            self.assertEqual(platform, asset["platform"])
            self.assertEqual("binary", asset["kind"])
        self.assertTrue(report["architecture"]["releaseAssetGap"])
        self.assertFalse(report["architecture"]["latestReleaseHasArm64Asset"])
        self.assertEqual("unconfirmed", report["architecture"]["windowsArm64Availability"])
        self.assertEqual([], report["architecture"]["windowsArm64Evidence"])
        self.assertEqual(1, len([risk for risk in report["risks"] if risk["id"] == "release-gap"]))

    def test_sources_checksums_and_ambiguous_archives_are_not_binaries(self):
        sources = ["app-win-arm64-source.zip", "app-win-arm64-src.7z"]
        checksums = ["app-win-arm64.zip.sha256", "SHA256SUMS", "app-win-arm64.exe.sig"]
        unknown = ["app-arm64.zip", "app-x64.7z", "app.nupkg", "app-win-arm64-symbols.zip", "app-win-arm64.pdb.zip"]
        report = self.assessment([self.release([*sources, *checksums, *unknown])])
        summary = report["release"]
        self.assertEqual(10, len(summary["latest"]["assets"]))
        self.assertEqual(sources, summary["sourceAssets"])
        self.assertEqual(checksums, summary["checksumAssets"])
        self.assertEqual(unknown, summary["unknownAssets"])
        self.assertEqual([], summary["windowsAssets"])
        self.assertEqual([], summary["arm64Assets"])
        self.assertEqual([], summary["x64Assets"])
        self.assertEqual([], summary["windowsArm64Assets"])
        self.assertFalse(report["architecture"]["releaseAssetGap"])
        self.assertEqual("unknown", report["architecture"]["releaseAssetGapStatus"])
        self.assertEqual("unconfirmed", report["architecture"]["windowsArm64Availability"])

    def test_unlabeled_windows_binary_architecture_is_unknown(self):
        report = self.assessment([self.release(["app-setup.exe"])])
        self.assertEqual(["app-setup.exe"], report["release"]["windowsAssets"])
        self.assertEqual([], report["release"]["windowsArm64Assets"])
        self.assertEqual([], report["release"]["windowsX64Assets"])
        self.assertEqual(1, len(report["release"]["latest"]["assets"]))
        self.assertEqual("unknown", report["release"]["latest"]["assets"][0]["architecture"])
        self.assertFalse(report["architecture"]["releaseAssetGap"])
        self.assertEqual("unknown", report["architecture"]["releaseAssetGapStatus"])

    def test_source_and_ambiguous_arm_assets_cannot_close_known_x64_gap(self):
        for name in ("app-arm64.zip", "app-win-arm64-source.zip", "app-win-arm64.zip.sha512"):
            with self.subTest(name=name):
                report = self.assessment([self.release(["app-win-x64.zip", name])])
                self.assertEqual(2, len(report["release"]["latest"]["assets"]))
                self.assertEqual([], report["release"]["windowsArm64Assets"])
                self.assertTrue(report["architecture"]["releaseAssetGap"])

    def test_conflicting_platform_and_architecture_labels_stay_unknown(self):
        cases = [
            ("app-linux-arm64.exe", "unknown", "arm64", "unknown"),
            ("app-win-macos-arm64.zip", "unknown", "arm64", "unknown"),
            ("app-win-x64-arm64.zip", "windows", "unknown", "binary"),
            ("app-win-arm64ec.zip", "windows", "arm64ec", "binary"),
            ("app-darwin-arm64.zip", "macos", "arm64", "binary"),
            ("app-win-x86_64.zip", "windows", "x64", "binary"),
        ]
        self.assertEqual(6, len(cases))
        for name, platform, architecture, kind in cases:
            with self.subTest(name=name):
                self.assertEqual(
                    {"platform": platform, "architecture": architecture, "kind": kind},
                    MODULE.classify_release_asset(name),
                )

    def test_no_releases_is_unknown_not_proof_of_a_port_or_gap(self):
        report = self.assessment([])
        summary = report["release"]
        self.assertIsNone(summary["latest"])
        self.assertEqual(0, summary["totalAssetDownloads"])
        self.assertEqual({"stable": None, "prerelease": None}, summary["channels"])
        self.assertEqual(0, summary["coverage"]["returnedReleaseCount"])
        self.assertEqual("provided", summary["coverage"]["collectionStatus"])
        self.assertFalse(summary["coverage"]["historyComplete"])
        self.assertEqual([], summary["relatedRepositories"])
        self.assertEqual(3, len(summary["unsearchedChannels"]))
        self.assertFalse(report["architecture"]["releaseAssetGap"])
        self.assertEqual("unknown", report["architecture"]["releaseAssetGapStatus"])
        self.assertEqual("unconfirmed", report["architecture"]["windowsArm64Availability"])
        self.assertIn("availability is unconfirmed", report["recommendation"])

    def test_stable_gap_and_prerelease_arm_evidence_remain_separate(self):
        report = self.assessment([
            self.release(["app-win-arm64.zip"], "v2-preview", prerelease=True),
            self.release(["app-win-x64.zip"], "v1"),
        ])
        summary = report["release"]
        self.assertEqual("v1", summary["latest"]["tag"])
        self.assertEqual("stable", summary["latest"]["channel"])
        self.assertEqual("v1", summary["channels"]["stable"]["latest"]["tag"])
        self.assertEqual("v2-preview", summary["channels"]["prerelease"]["latest"]["tag"])
        self.assertEqual(2, summary["coverage"]["returnedReleaseCount"])
        self.assertEqual(1, summary["coverage"]["stableReleaseCount"])
        self.assertEqual(1, summary["coverage"]["prereleaseCount"])
        self.assertTrue(report["architecture"]["releaseAssetGap"])
        self.assertFalse(report["architecture"]["latestReleaseHasArm64Asset"])
        self.assertEqual("observed", report["architecture"]["windowsArm64Availability"])
        self.assertEqual(1, len(report["architecture"]["windowsArm64Evidence"]))
        self.assertEqual(
            {
                "repository": "example/app",
                "tag": "v2-preview",
                "channel": "prerelease",
                "url": "https://github.com/example/app/releases/tag/v2-preview",
                "assets": ["app-win-arm64.zip"],
            },
            report["architecture"]["windowsArm64Evidence"][0],
        )
        self.assertIn("before proposing a new port", report["recommendation"])
        self.assertNotIn("Strong native ARM64 candidate", report["recommendation"])

    def test_prerelease_fallback_is_labeled_and_drafts_are_excluded(self):
        summary = MODULE.release_summary([
            self.release(["app-win-arm64.zip"], "draft", draft=True),
            self.release(["app-win-x64.zip"], "preview", prerelease=True),
        ])
        self.assertEqual("preview", summary["latest"]["tag"])
        self.assertEqual("prerelease", summary["latest"]["channel"])
        self.assertEqual(2, summary["coverage"]["returnedReleaseCount"])
        self.assertEqual(1, summary["coverage"]["draftsExcluded"])
        self.assertEqual(0, summary["coverage"]["stableReleaseCount"])
        self.assertEqual(1, summary["coverage"]["prereleaseCount"])
        self.assertIsNone(summary["channels"]["stable"])
        self.assertEqual([], summary["observedWindowsArm64Releases"])
        draft_only = MODULE.release_summary([self.release(["app-win-arm64.zip"], draft=True)])
        self.assertIsNone(draft_only["latest"])
        self.assertEqual(1, draft_only["coverage"]["draftsExcluded"])
        self.assertEqual([], draft_only["windowsArm64Assets"])

    def test_older_arm_evidence_does_not_close_selected_release_gap(self):
        report = self.assessment([
            self.release(["app-win-x64.zip"], "v2"),
            self.release(["app-win-arm64.zip"], "v1"),
        ])
        self.assertEqual("v2", report["release"]["latest"]["tag"])
        self.assertTrue(report["architecture"]["releaseAssetGap"])
        self.assertEqual(1, len(report["architecture"]["windowsArm64Evidence"]))
        self.assertEqual("v1", report["architecture"]["windowsArm64Evidence"][0]["tag"])
        self.assertEqual("observed", report["architecture"]["windowsArm64Availability"])

    def test_explicit_related_release_repository_does_not_replace_primary(self):
        report = self.assessment(
            [self.release(["app-win-x64.zip"])],
            related_releases={"example/DevBuilds": [
                self.release(["app-win-arm64.zip"], "dev", prerelease=True)
            ]},
        )
        self.assertEqual("example/app", report["repository"]["fullName"])
        self.assertEqual("v1", report["release"]["latest"]["tag"])
        self.assertTrue(report["architecture"]["releaseAssetGap"])
        self.assertEqual(1, len(report["release"]["relatedRepositories"]))
        related = report["release"]["relatedRepositories"][0]
        self.assertEqual("example/DevBuilds", related["coverage"]["repository"])
        self.assertEqual("dev", related["latest"]["tag"])
        self.assertEqual(1, len(report["architecture"]["windowsArm64Evidence"]))
        self.assertEqual("example/DevBuilds", report["architecture"]["windowsArm64Evidence"][0]["repository"])
        self.assertEqual("observed", report["architecture"]["windowsArm64Availability"])
        self.assertNotIn("Strong native ARM64 candidate", report["recommendation"])

    def test_collection_failure_is_not_an_empty_successful_response(self):
        warnings = []
        with mock.patch.object(MODULE, "github_get", side_effect=RuntimeError("unavailable")) as get:
            releases, coverage = MODULE.collect_releases("example/DevBuilds", None, warnings)
        get.assert_called_once_with("/repos/example/DevBuilds/releases?per_page=10", None)
        self.assertEqual([], releases)
        self.assertEqual(["unavailable"], warnings)
        self.assertEqual({"collection_status": "failed", "release_limit": 10}, coverage)
        summary = MODULE.release_summary(releases, repository="example/DevBuilds", **coverage)
        self.assertEqual("failed", summary["coverage"]["collectionStatus"])
        self.assertEqual("github-release-page", summary["coverage"]["scope"])
        self.assertEqual(10, summary["coverage"]["releaseLimit"])
        with mock.patch.object(MODULE, "github_get", return_value=[]):
            _, successful_coverage = MODULE.collect_releases("example/DevBuilds", None, [])
        self.assertEqual({"collection_status": "collected", "release_limit": 10}, successful_coverage)

    def test_release_repository_flag_accepts_only_safe_slugs(self):
        self.assertEqual("example/Dev.Builds", MODULE.parse_release_repository("example/Dev.Builds"))
        for value in (
            "https://github.com/example/app", "example/app?x=1", "../app", "example/..",
            "example/.", "example/app/extra", "example\\app", "-example/app", "example/app#fragment",
        ):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    MODULE.parse_release_repository(value)
        with mock.patch.object(MODULE.sys, "argv", [
            "woa_scout.py", "--repo", "example/app", "--output", "unused",
            "--release-repo", "example/app?x=1",
        ]), mock.patch.object(MODULE, "github_get") as get, mock.patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as error:
                MODULE.main()
        self.assertEqual(2, error.exception.code)
        get.assert_not_called()

    def test_cli_collects_only_explicit_repositories_and_deduplicates(self):
        calls = []

        def get(path, token):
            calls.append(path)
            if path == "/repos/example/app":
                return {"full_name": "example/app", "default_branch": "main"}
            if path == "/repos/example/app/commits/main":
                return {"sha": "a" * 40}
            if path == "/repos/example/app/releases?per_page=10":
                return [self.release(["app-win-x64.zip"])]
            if path == "/repos/example/DevBuilds/releases?per_page=10":
                return [self.release(["app-win-arm64.zip"], "dev", prerelease=True)]
            if path.startswith("/search/issues?"):
                return {"items": []}
            if path == f"/repos/example/app/git/trees/{'a' * 40}?recursive=1":
                return {"tree": []}
            self.fail(f"Unexpected network access: {path}")

        with mock.patch.object(MODULE.sys, "argv", [
            "woa_scout.py", "--repo", "example/app", "--output", "unused",
            "--release-repo", "example/DevBuilds", "--release-repo", "EXAMPLE/devbuilds",
            "--release-repo", "example/app",
        ]), mock.patch.object(MODULE, "github_get", side_effect=get), \
            mock.patch.object(MODULE.pathlib.Path, "mkdir"), \
            mock.patch.object(MODULE.pathlib.Path, "write_text") as write, \
            mock.patch.object(MODULE, "fetch_remote_contents", return_value={}) as fetch, \
            mock.patch.object(MODULE, "write_markdown"), \
            mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(0, MODULE.main())
        self.assertEqual(7, len(calls))
        fetch.assert_called_once_with("example", "app", "a" * 40, [], [])
        self.assertEqual(
            ["/repos/example/app/releases?per_page=10", "/repos/example/DevBuilds/releases?per_page=10"],
            [path for path in calls if "/releases?" in path],
        )
        write.assert_called_once()
        report = json.loads(write.call_args.args[0])
        self.assertEqual("collected", report["release"]["coverage"]["collectionStatus"])
        self.assertEqual(10, report["release"]["coverage"]["releaseLimit"])
        self.assertEqual(1, len(report["release"]["relatedRepositories"]))
        self.assertEqual("collected", report["release"]["relatedRepositories"][0]["coverage"]["collectionStatus"])
        self.assertEqual("observed", report["architecture"]["windowsArm64Availability"])
        self.assertEqual("not-assessed", report["deliveryWindow"]["fit"])
        self.assertEqual("a" * 40, report["repository"]["analyzedCommit"])

    def test_unresolved_remote_commit_stops_before_source_or_release_collection(self):
        with mock.patch.object(MODULE.sys, "argv", [
            "woa_scout.py", "--repo", "example/app", "--output", "unused",
        ]), mock.patch.object(MODULE, "github_get", side_effect=[
            {"full_name": "example/app", "default_branch": "main"}, {"sha": "abc"},
        ]) as get, mock.patch.object(MODULE.pathlib.Path, "write_text") as write:
            with self.assertRaisesRegex(ValueError, "resolved immutable commit"):
                MODULE.main()
        self.assertEqual(2, get.call_count)
        write.assert_not_called()

    def test_markdown_reports_channel_boundaries_and_planning_uncertainty(self):
        report = self.assessment(
            [self.release(["app-win-x64.zip"], "preview", prerelease=True)],
            related_releases={"example/DevBuilds": [self.release(["app-win-arm64.zip"], "dev")]},
        )
        path = mock.Mock()
        MODULE.write_markdown(report, path)
        path.write_text.assert_called_once()
        markdown = path.write_text.call_args.args[0]
        self.assertIn("Selected release: `preview` (prerelease)", markdown)
        self.assertNotIn("Latest stable release:", markdown)
        self.assertEqual(1, markdown.count("## Release Coverage"))
        self.assertIn("`example/DevBuilds`", markdown)
        self.assertIn("full history not established", markdown)
        self.assertIn("not delivery commitments", markdown)
        self.assertIn("Related repositories not explicitly supplied", markdown)
        self.assertIn("no binary architecture or functionality was verified", markdown)

    def test_schema_describes_generated_release_metadata(self):
        schema_path = MODULE_PATH.parents[1] / "schemas" / "woa-scout.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        report = self.assessment([self.release(["app-win-arm64.zip"])])
        self.assertEqual(schema["properties"]["schemaVersion"]["const"], report["schemaVersion"])
        definitions = schema["$defs"]
        self.assertEqual(
            set(definitions["releaseSummary"]["properties"]),
            set(report["release"]),
        )
        self.assertEqual(
            set(definitions["releaseCoverage"]["required"]),
            set(report["release"]["coverage"]),
        )
        self.assertEqual(1, len(report["release"]["latest"]["assets"]))
        asset = report["release"]["latest"]["assets"][0]
        self.assertEqual("app-win-arm64.zip", asset["name"])
        for field in ("kind", "architecture", "platform"):
            self.assertIn(asset[field], definitions["releaseAsset"]["properties"][field]["enum"])
        self.assertEqual(1, len(report["architecture"]["windowsArm64Evidence"]))
        self.assertEqual(
            set(definitions["releaseEvidence"]["required"]),
            set(report["architecture"]["windowsArm64Evidence"][0]),
        )
        for field in ("releaseAssetGapStatus", "windowsArm64Availability"):
            self.assertIn(report["architecture"][field], schema["properties"]["architecture"]["properties"][field]["enum"])

    def test_root_level_tests_directory_is_detected(self):
        self.assertTrue(MODULE.detect_tests(["tests/App.Tests.csproj"]))
        self.assertTrue(MODULE.detect_tests(["App.Tests/App.Tests.csproj"]))
        self.assertFalse(MODULE.detect_tests(["src/App.csproj"]))


if __name__ == "__main__":
    unittest.main()
