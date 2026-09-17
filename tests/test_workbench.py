import argparse
import asyncio
import contextlib
import copy
import hashlib
import io
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

from workbench import agent, cli, core, evidence, github_runner, runner


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.store = core.Store(self.root / "runs")
        self.state = self.store.create("example/desktop")
        self.run = self.store.path(self.state["id"])
        self.source = self.run / "source"
        self.source.mkdir()
        self.project = self.source / "Desktop.csproj"
        self.project.write_text(
            '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>WinExe</OutputType>'
            '<TargetFramework>net9.0-windows</TargetFramework><UseWPF>true</UseWPF>'
            '</PropertyGroup></Project>', encoding="utf-8")
        assessment = self.run / "assessment" / "assessment.json"
        core.write_json(assessment, {"repository": {"fullName": "example/desktop", "analyzedCommit": "a" * 40,
                                                   "workingTreeDirty": False}})
        self.plan = {
            "schemaVersion": 1, "runId": self.state["id"], "repository": "example/desktop",
            "sourceCommit": "a" * 40, "sourceHash": core.tree_hash(self.source),
            "project": "Desktop.csproj", "framework": "net9.0-windows", "assemblyName": "Desktop",
            "sdkVersion": "9.0.318", "execution": "github-hosted-windows",
            "architectures": ["win-x64", "win-arm64"],
            "commands": {rid: core.commands_for("Desktop.csproj", "net9.0-windows", rid, f"<isolated-output>/{rid}")
                         for rid in ("win-x64", "win-arm64")},
            "sourceIsolation": "worktree-per-runtime", "testCommands": [],
            "assessmentSha256": core.file_hash(assessment),
            "scope": "Test fixture, not a built app.",
            "runner": {"repository": "example/toolkit", "ref": "refs/tags/runner-test", "commit": "b" * 40,
                       "workflow": "repo_to_arm_build.yml", "rulesetId": 42},
        }
        self.state.update({"sourceCommit": "a" * 40, "sourceHash": self.plan["sourceHash"],
                           "plan": self.plan, "planHash": core.digest(self.plan), "approvalKind": "source-plan"})
        core.write_json(self.run / "plan.json", self.plan)
        self.store.save(self.state)

    def tearDown(self):
        self.temporary.cleanup()

    def test_repository_parser_rejects_extra_paths_and_unsafe_hosts(self):
        self.assertEqual("example/desktop", core.parse_repository("https://github.com/example/desktop.git"))
        for value in ("https://evil.example/example/desktop", "example/desktop/tree/main", "../desktop",
                      "https://github.com/example/desktop?token=anything", "example/desktop;echo"):
            with self.subTest(value=value), self.assertRaises(core.WorkbenchError):
                core.parse_repository(value)

    def test_scoped_paths_reject_traversal_devices_and_git(self):
        for value in ("../outside", r"..\outside", "/absolute", r"C:\outside", "NUL.txt", ".git/config", ".git./config", "folder./file"):
            with self.subTest(value=value), self.assertRaises(core.WorkbenchError):
                core.below(self.source, value)
        self.assertEqual(self.project, core.below(self.source, "Desktop.csproj", must_exist=True))

    def test_project_discovery_reads_literal_parent_framework(self):
        self.project.write_text(
            '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>WinExe</OutputType>'
            '<UseWPF>true</UseWPF></PropertyGroup></Project>', encoding="utf-8")
        (self.source / "Directory.Build.props").write_text(
            "<Project><PropertyGroup><TargetFramework>net9.0-windows</TargetFramework></PropertyGroup></Project>",
            encoding="utf-8")
        projects = core.discover_projects(self.source)
        self.assertEqual(1, len(projects))
        self.assertEqual("net9.0-windows", core.choose_project(projects, None)["framework"])

    def test_approval_is_bound_to_current_source_and_plan(self):
        with self.assertRaises(core.WorkbenchError):
            core.require_approval(self.store, self.state, "source-plan")
        approved = core.approve(self.store, self.state["id"], self.state["planHash"])
        core.require_approval(self.store, approved, "source-plan")
        self.project.write_text(self.project.read_text() + "\n", encoding="utf-8")
        with self.assertRaises(core.WorkbenchError):
            core.require_approval(self.store, approved, "source-plan")

    def test_wrong_approval_hash_does_not_approve(self):
        with self.assertRaises(core.WorkbenchError):
            core.approve(self.store, self.state["id"], "0" * 64)
        self.assertEqual([], self.store.load(self.state["id"])["approvals"])

    def test_modified_scout_evidence_invalidates_approval(self):
        self.state = core.approve(self.store, self.state["id"], self.state["planHash"])
        core.write_json(self.run / "assessment" / "assessment.json", {"modified": True})
        with self.assertRaisesRegex(core.WorkbenchError, "Scout evidence changed"):
            core.require_approval(self.store, self.state, "source-plan")
        with self.assertRaisesRegex(core.WorkbenchError, "Scout evidence changed"):
            core.approve(self.store, self.state["id"], self.state["planHash"])

    def test_operation_lock_is_released_and_not_a_stale_file_gate(self):
        with self.store.lock(self.state["id"]):
            with self.assertRaises(core.WorkbenchError):
                with self.store.lock(self.state["id"]):
                    self.fail("A second mutation acquired the same run.")
        with self.store.lock(self.state["id"]):
            self.assertTrue((self.run / ".operation.lock").exists())

    def test_scoped_replacement_preserves_bom_and_crlf(self):
        path = self.source / "App.cs"
        path.write_bytes(b"\xef\xbb\xbfclass App {\r\n  const int Count = 1;\r\n}\r\n")
        agent.replace_source(self.source, "App.cs", "Count = 1;", "Count = 2;")
        self.assertEqual(b"\xef\xbb\xbfclass App {\r\n  const int Count = 2;\r\n}\r\n", path.read_bytes())
        with self.assertRaises(core.WorkbenchError):
            agent.replace_source(self.source, "App.cs", "not present", "replacement")

    def test_agent_cannot_write_runner_or_host_files(self):
        for path in ("../run.json", ".github/workflows/build.yml", ".copilot/instructions.md", "bin/loader.cs", "payload.exe"):
            with self.subTest(path=path), self.assertRaises(core.WorkbenchError):
                agent.create_source(self.source, path, "blocked")
        agent.create_source(self.source, "Properties/ArmSupport.props", "<Project />")
        self.assertEqual("<Project />", agent.read_source(self.source, "Properties/ArmSupport.props"))

    def test_agent_session_errors_are_explicit_and_listener_is_released(self):
        class Session:
            def __init__(self, events):
                self.events = events
                self.listener = None
                self.unsubscribed = False

            def on(self, callback):
                self.listener = callback
                def unsubscribe():
                    self.unsubscribed = True
                return unsubscribe

            async def send(self, prompt):
                for kind, data in self.events:
                    self.listener(SimpleNamespace(type=kind, data=SimpleNamespace(**data)))

        successful = Session([("assistant.message", {"content": "completed"}), ("session.idle", {})])
        self.assertEqual("completed", asyncio.run(agent.send_agent_prompt(successful, "fixture")))
        self.assertTrue(successful.unsubscribed)
        failed = Session([("assistant.message", {"content": "partial"}), ("session.error", {"message": "transport failed"})])
        with self.assertRaisesRegex(core.WorkbenchError, "transport failed"):
            asyncio.run(agent.send_agent_prompt(failed, "fixture"))
        self.assertTrue(failed.unsubscribed)
        interrupted = Session([])
        with self.assertRaises(TimeoutError):
            asyncio.run(agent.send_agent_prompt(interrupted, "fixture", timeout=0.001))
        self.assertTrue(interrupted.unsubscribed)

    def test_runner_profile_accepts_only_exact_supported_commands(self):
        self.assertEqual("baseline", runner.validate_plan(self.plan, core.digest(self.plan), ""))
        modified = copy.deepcopy(self.plan)
        modified["commands"]["win-arm64"][1].append("-p:CustomCommand=anything")
        with self.assertRaises(core.WorkbenchError):
            runner.validate_plan(modified, core.digest(modified), "")
        with self.assertRaises(core.WorkbenchError):
            runner.validate_plan(self.plan, "0" * 64, "")

    def test_runner_patch_hash_and_paths_are_bound(self):
        patch = "--- a/Desktop.csproj\n+++ b/Desktop.csproj\n@@ -1 +1 @@\n-old\n+new\n"
        plan = {**self.plan, "patchSha256": hashlib.sha256(patch.encode()).hexdigest(), "workingSourceHash": "c" * 64}
        self.assertEqual("candidate", runner.validate_plan(plan, core.digest(plan), patch))
        with self.assertRaises(core.WorkbenchError):
            runner.validate_plan(plan, core.digest(plan), patch + "modified")
        escaped = patch.replace("b/Desktop.csproj", "b/../runner.py")
        plan["patchSha256"] = hashlib.sha256(escaped.encode()).hexdigest()
        with self.assertRaises(core.WorkbenchError):
            runner.validate_plan(plan, core.digest(plan), escaped)

    def test_zip_extraction_rejects_escape_case_collision_and_links(self):
        for name, entries in (
            ("escape", [("../outside.txt", b"no")]),
            ("case", [("App.dll", b"a"), ("app.dll", b"b")]),
        ):
            archive = self.root / f"{name}.zip"
            with zipfile.ZipFile(archive, "w") as zipped:
                for filename, content in entries:
                    zipped.writestr(filename, content)
            with self.assertRaises(core.WorkbenchError):
                core.safe_extract(archive, self.root / f"{name}-out")
        archive = self.root / "link.zip"
        with zipfile.ZipFile(archive, "w") as zipped:
            info = zipfile.ZipInfo("link")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zipped.writestr(info, "../outside")
        with self.assertRaises(core.WorkbenchError):
            core.safe_extract(archive, self.root / "link-out")

    def test_zip_extraction_and_manifest_cover_real_files(self):
        archive = self.root / "valid.zip"
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("app/App.dll", b"fixture-data")
            zipped.writestr("app/settings.json", b"{}")
        output = self.root / "valid"
        core.safe_extract(archive, output)
        files = core.tree_manifest(output)
        self.assertEqual(2, len(files))
        self.assertEqual(["app/App.dll", "app/settings.json"], [item["path"] for item in files])
        self.assertEqual(hashlib.sha256(b"fixture-data").hexdigest(), files[0]["sha256"])

    def test_events_have_monotonic_sequences_and_real_state(self):
        self.store.event(self.state, "port", "running", "Started")
        self.store.event(self.state, "port", "failed", "Explicit failure")
        events = [json.loads(line) for line in (self.run / "events.jsonl").read_text().splitlines()]
        self.assertEqual(2, len(events))
        self.assertEqual([1, 2], [event["sequence"] for event in events])
        self.assertEqual("failed", self.store.load(self.state["id"])["status"])

    def test_runner_rejects_mutable_tags_and_ruleset_bypasses(self):
        reference = self.plan["runner"]["ref"]
        rule = {"id": 42, "target": "tag", "enforcement": "active", "bypass_actors": [],
                "conditions": {"ref_name": {"include": [reference], "exclude": []}},
                "rules": [{"type": "update"}, {"type": "deletion"}]}
        config = {"runnerRepository": "example/toolkit", "runnerRef": reference, "runnerRulesetId": 42}
        responses = [rule, {"ref": reference, "object": {"type": "commit", "sha": "b" * 40}}, {"type": "file"}]
        with patch.object(github_runner, "gh", side_effect=responses) as api:
            self.assertEqual(self.plan["runner"], github_runner.pinned_runner(config))
            self.assertEqual(3, api.call_count)
        for key, value in (("enforcement", "disabled"), ("bypass_actors", [{"actor_id": 1}]),
                           ("rules", [{"type": "deletion"}]),
                           ("conditions", {"ref_name": {"include": ["refs/tags/*"], "exclude": []}})):
            changed = {**rule, key: value}
            with self.subTest(key=key), patch.object(github_runner, "gh", return_value=changed), self.assertRaises(core.WorkbenchError):
                github_runner.pinned_runner(config)
        with patch.object(github_runner, "gh") as api, self.assertRaises(core.WorkbenchError):
            github_runner.pinned_runner({**config, "runnerRef": "main"})
        api.assert_not_called()

    def test_unconfirmed_dispatch_is_journaled_and_never_automatically_resent(self):
        config = {}
        def api(arguments_config, arguments, payload=None):
            if "--method" in arguments:
                raise core.WorkbenchError("connection ended after request")
            return {"workflow_runs": []}
        with patch.object(github_runner, "gh", side_effect=api) as calls:
            with self.assertRaisesRegex(core.WorkbenchError, "connection ended"):
                github_runner.dispatch_build(self.store, self.state, config, self.plan, "", "baseline", "baseline")
            saved = self.store.load(self.state["id"])
            self.assertEqual(self.state["pendingDispatch"], saved["pendingDispatch"])
            with self.assertRaisesRegex(core.WorkbenchError, "No duplicate"):
                github_runner.dispatch_build(self.store, saved, config, self.plan, "", "baseline", "baseline")
        self.assertEqual(2, calls.call_count)
        self.assertEqual(1, sum("--method" in call.args[1] for call in calls.call_args_list))

    def test_completed_run_artifact_failure_resumes_without_a_new_dispatch(self):
        self.state = core.approve(self.store, self.state["id"], self.state["planHash"])
        self.state["githubRun"] = {"id": 17, "repository": "example/toolkit", "operation": "baseline",
                                  "invocation": "fixture", "planHash": core.digest(self.plan), "conclusion": "success"}
        run_result = {"id": 17, "head_sha": "b" * 40, "status": "completed", "conclusion": "success",
                      "html_url": "https://github.com/example/toolkit/actions/runs/17"}
        def api(config, arguments, payload=None):
            if arguments[-1].endswith("/artifacts"):
                raise core.WorkbenchError("artifact download unavailable")
            return run_result
        with patch.object(github_runner, "pinned_runner", return_value=self.plan["runner"]), \
                patch.object(github_runner, "gh", side_effect=api) as calls:
            for _ in range(2):
                with self.assertRaisesRegex(core.WorkbenchError, "artifact download unavailable"):
                    github_runner.run_build(self.store, self.state, {})
        self.assertEqual(6, calls.call_count)
        self.assertFalse(any("--method" in call.args[1] for call in calls.call_args_list))
        self.assertEqual(17, self.state["githubRun"]["id"])

    def test_cli_lock_contention_never_changes_state_or_events(self):
        before = (self.run / "run.json").read_bytes()
        output = io.StringIO()
        args = ["repo-to-arm", "--root", str(self.store.root), "approve", self.state["id"],
                "--plan-hash", self.state["planHash"]]
        with self.store.lock(self.state["id"]), patch.object(sys, "argv", args), contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(1, cli.main())
        self.assertEqual(before, (self.run / "run.json").read_bytes())
        self.assertFalse((self.run / "events.jsonl").exists())
        self.assertIsNone(json.loads(output.getvalue())["run"])

    def test_cli_failure_is_persisted_under_the_operation_lock(self):
        seen = []
        actual_event = self.store.event

        def check_lock(*args, **kwargs):
            with self.assertRaises(core.WorkbenchError):
                with self.store.lock(self.state["id"]):
                    self.fail("Failure recording lost lock ownership.")
            seen.append(args[2])
            return actual_event(*args, **kwargs)

        args = argparse.Namespace(command="approve", run_id=self.state["id"], plan_hash="0" * 64)
        with patch.object(cli, "configuration", return_value={}), patch.object(self.store, "event", side_effect=check_lock):
            with self.assertRaises(core.WorkbenchError):
                cli.mutate(self.store, args)
        self.assertEqual(["failed"], seen)
        self.assertEqual("failed", self.store.load(self.state["id"])["status"])

    def initialize_git_source(self):
        home = self.run / "git-home"
        core.git(["init", "--initial-branch=main"], cwd=self.source, home=home)
        core.git(["add", "."], cwd=self.source, home=home)
        core.git(["-c", "user.name=Test Fixture", "-c", "user.email=fixture@example.invalid",
                  "commit", "-m", "Initial fixture"], cwd=self.source, home=home)
        commit = core.git(["rev-parse", "HEAD"], cwd=self.source, home=home)
        self.state["sourceCommit"] = self.plan["sourceCommit"] = commit
        self.state["sourceHash"] = self.plan["sourceHash"] = core.tree_hash(self.source)
        self.state["planHash"] = core.digest(self.plan)
        core.write_json(self.run / "plan.json", self.plan)
        self.store.save(self.state)

    def test_patch_replays_bom_crlf_empty_file_and_missing_final_newline(self):
        content = self.source / "App.cs"
        content.write_bytes(b"\xef\xbb\xbfclass App {\r\n  int x = 1;\r\n}\r\n\r\n")
        self.initialize_git_source()
        agent.replace_source(self.source, "App.cs", "x = 1;", "x = 2;")
        agent.create_source(self.source, "NoFinalNewline.txt", "line one\nline two")
        agent.create_source(self.source, "Empty.txt", "")
        state = core.finalize_port(self.store, self.state)
        replay = pathlib.Path(state["artifacts"]["patchVerification"])
        self.assertEqual(core.tree_hash(self.source), core.tree_hash(replay))
        self.assertEqual(content.read_bytes(), (replay / "App.cs").read_bytes())
        self.assertEqual(b"line one\nline two", (replay / "NoFinalNewline.txt").read_bytes())
        self.assertEqual(b"", (replay / "Empty.txt").read_bytes())
        self.assertEqual(4, len(core.tree_manifest(replay)))

    def test_interrupted_port_preserves_edits_and_requires_fresh_approval(self):
        self.initialize_git_source()
        self.state = core.approve(self.store, self.state["id"], self.state["planHash"])
        core.begin_port(self.store, self.state)
        agent.create_source(self.source, "Partial.props", "<Project />")
        self.assertTrue(core.reconcile_interrupted_port(self.store, self.state))
        self.assertEqual("interrupted-port", self.state["approvalKind"])
        with self.assertRaises(core.WorkbenchError):
            core.require_approval(self.store, self.state, "interrupted-port")
        self.state = core.approve(self.store, self.state["id"], self.state["planHash"])
        core.require_approval(self.store, self.state, "interrupted-port")
        partial_hash = self.state["planHash"]
        agent.replace_source(self.source, "Partial.props", "<Project />", "<Project></Project>")
        self.assertTrue(core.reconcile_interrupted_port(self.store, self.state))
        self.assertNotEqual(partial_hash, self.state["planHash"])
        with self.assertRaises(core.WorkbenchError):
            core.require_approval(self.store, self.state, "interrupted-port")
        self.assertIn("Partial.props", pathlib.Path(self.state["artifacts"]["partialPatch"]).read_text())
        self.assertNotIn("verification", self.state)

    def prepare_candidate_evidence(self):
        package = self.run / "arm64"
        package.mkdir()
        (package / "Desktop.exe").write_bytes(b"package identity fixture, never executed")
        self.state["assemblyName"] = "Desktop"
        core.write_json(self.run / "build-plan.json", self.plan)
        receipt = self.run / "receipt.json"
        core.write_json(receipt, {"fixture": "not a real build"})
        self.state["artifacts"]["buildReceipt"] = str(receipt)
        self.state["githubRun"] = {"id": 17, "operation": "candidate", "conclusion": "success"}
        identity = {"planHash": self.state["planHash"], "githubRunId": 17, "runnerCommit": "b" * 40,
                    "repository": "example/toolkit", "receiptSha256": core.file_hash(receipt)}
        self.state["candidateBuild"] = identity
        self.state["activePackage"] = str(package)
        report = self.run / "architecture.json"
        core.write_json(report, {"RootPath": str(package), "ExpectedArchitecture": "arm64", "FileCount": 1,
                                 "InvalidCount": 0, "Files": [{"Path": "Desktop.exe", "Valid": True, "Machine": "Arm64"}]})
        self.state["verification"] = {"passed": True, "report": str(report), "reportSha256": core.file_hash(report),
                                      "packageTreeHash": core.tree_hash(package), "packageRoot": str(package),
                                      "candidate": identity}
        return package, report

    def test_evidence_rejects_stale_candidate_report_and_device_package(self):
        package, report = self.prepare_candidate_evidence()
        self.assertEqual(1, evidence.require_verification(self.store, self.state)["FileCount"])
        device = self.run / "input-device.json"
        core.write_json(device, {"processId": 123, "processStartedAt": "2026-09-17T15:00:00Z",
                                "candidate": self.state["candidateBuild"], "packageTreeHash": core.tree_hash(package),
                                "scenarios": [{"name": "fixture", "passed": True}]})
        with patch.object(evidence, "collect_process_proof", return_value={"result": {"processStartedAt": "2026-09-17T15:00:00Z"}}) as probe:
            evidence.record_device_evidence(self.store, self.state, device)
            probe.assert_called_once_with(self.store, self.state, 123, {})
        evidence.export_evidence(self.store, self.state)
        self.assertEqual("device-verified", core.read_json(pathlib.Path(self.state["artifacts"]["evidence"]))["verificationLevel"])
        self.state["githubRun"]["id"] = 18
        with self.assertRaises(core.WorkbenchError):
            evidence.export_evidence(self.store, self.state)
        self.state["githubRun"]["id"] = 17
        (package / "new-file.txt").write_text("package mutated after verification")
        data = core.read_json(device)
        data["packageTreeHash"] = core.tree_hash(package)
        core.write_json(device, data)
        with self.assertRaises(core.WorkbenchError):
            evidence.record_device_evidence(self.store, self.state, device)
        (package / "new-file.txt").unlink()
        report.write_text("{}")
        with self.assertRaises(core.WorkbenchError):
            evidence.require_verification(self.store, self.state)

    def test_recovery_rejects_old_fault_for_a_new_active_package(self):
        package, report = self.prepare_candidate_evidence()
        data = core.read_json(report)
        data.update({"InvalidCount": 1, "Files": [{"Path": "wrong.dll", "Valid": False, "Machine": "Amd64"}]})
        core.write_json(report, data)
        self.state["verification"].update({"passed": False, "reportSha256": core.file_hash(report)})
        self.state["fault"] = {"declaredDemonstration": True, "candidate": self.state["candidateBuild"],
                               "faultPackage": str(package / "old"), "faultTreeHash": core.tree_hash(package)}
        with self.assertRaisesRegex(core.WorkbenchError, "declared package-contamination"):
            asyncio.run(evidence.repair_package(self.store, self.state, {}))

    def test_new_candidate_invalidates_all_previous_evidence_authority(self):
        self.prepare_candidate_evidence()
        self.state["fault"] = {"old": True}
        self.state["recovery"] = {"old": True}
        self.state["deviceEvidence"] = {"old": True}
        core.invalidate_package_state(self.state)
        for key in ("verification", "candidateBuild", "activePackage", "fault", "recovery", "deviceEvidence"):
            with self.subTest(key=key):
                self.assertNotIn(key, self.state)
        self.assertEqual({}, self.state["totals"])

    def test_runner_uses_exact_sdk_and_separate_source_trees_per_runtime(self):
        self.initialize_git_source()
        agent.create_source(self.source, "Build.props", "<Project />")
        self.state = core.finalize_port(self.store, self.state)
        plan = core.read_json(self.run / "build-plan.json")
        directory = self.root / "worker"
        directory.mkdir()
        core.write_json(directory / "plan.json", plan)
        core.write_json(directory / "invocation.json", {
            "approvedPlanHash": core.digest(plan), "operation": "candidate", "runId": self.state["id"],
            "toolkitCommit": plan["runner"]["commit"], "githubRunId": "17",
        })
        (directory / "port.patch").write_bytes((self.run / "port.patch").read_bytes())
        dotnet = self.root / "sdk" / "dotnet.exe"
        dotnet.parent.mkdir()
        dotnet.write_bytes(b"test boundary; never execute")
        real_git = core.git
        real_run = subprocess.run
        builds = []

        def local_git(arguments, **kwargs):
            arguments = list(arguments)
            if arguments[0] == "clone":
                arguments[-2] = str(self.source)
            return real_git(arguments, **kwargs)

        def simulate_compiler(arguments, **kwargs):
            if arguments[0] != str(dotnet):
                return real_run(arguments, **kwargs)
            working = pathlib.Path(kwargs["cwd"])
            runtime = arguments[arguments.index("--runtime") + 1]
            self.assertEqual(directory / "sources" / runtime, working)
            self.assertEqual(str(dotnet.parent), kwargs["env"]["DOTNET_ROOT"])
            builds.append((arguments[1], runtime, working))
            if arguments[1] == "restore":
                self.assertFalse((working / "obj").exists())
                (working / "obj").mkdir()
                (working / "obj" / "runtime.txt").write_text(runtime)
            else:
                self.assertEqual(runtime, (working / "obj" / "runtime.txt").read_text())
                output = pathlib.Path(arguments[arguments.index("--output") + 1])
                output.mkdir(parents=True)
                (output / "Desktop.exe").write_bytes(b"not a real application")
            return subprocess.CompletedProcess(arguments, 0)

        environment = {"GITHUB_ACTIONS": "true", "RUNNER_TEMP": str(self.root), "RTA_DOTNET_PATH": str(dotnet)}
        with patch.dict(os.environ, environment), patch.object(runner, "git", side_effect=local_git), \
                patch.object(subprocess, "run", side_effect=simulate_compiler), \
                patch.object(subprocess, "check_output", return_value="9.0.318\n") as version, \
                contextlib.redirect_stdout(io.StringIO()):
            runner.execute(directory)
        version.assert_called_once_with([str(dotnet), "--version"], cwd=directory / "source", text=True)
        self.assertEqual(4, len(builds))
        self.assertEqual([("restore", "win-x64"), ("publish", "win-x64"),
                          ("restore", "win-arm64"), ("publish", "win-arm64")], [(a, b) for a, b, _ in builds])
        receipt = core.read_json(directory / "artifacts" / "build-receipt.json")
        self.assertEqual(2, len(receipt["packages"]))
        self.assertEqual(["win-x64", "win-arm64"], [item["runtime"] for item in receipt["packages"]])
        self.assertEqual("no-test-projects-detected", receipt["testCoverage"])
        self.assertFalse((directory / "source" / "obj").exists())


if __name__ == "__main__":
    unittest.main()
