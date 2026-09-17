from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import subprocess
import sys

from .core import (
    Store, WorkbenchError, begin_port, capture_interrupted_port, finalize_port, parse_repository,
    prepare_source, read_json, reconcile_interrupted_port, require_approval, write_json,
)


def configuration(store: Store) -> dict:
    path = store.root / "config.json"
    if not path.is_file():
        raise WorkbenchError("Configure an isolated runner first with the configure command.")
    config = read_json(path)
    allowed = {"schemaVersion", "runnerRepository", "runnerRef", "runnerRulesetId", "runnerGhConfigDir",
               "copilotGhConfigDir", "copilotUser", "ghPath", "pwshPath", "model"}
    if not isinstance(config, dict) or set(config) - allowed or config.get("schemaVersion") != 1:
        raise WorkbenchError("Unsupported workbench configuration.")
    parse_repository(config["runnerRepository"])
    return config


def mutate(store: Store, args: argparse.Namespace) -> dict:
    with store.lock(args.run_id):
        state = store.load(args.run_id)
        try:
            if reconcile_interrupted_port(store, state):
                return state
            config = configuration(store)
            if args.command == "approve":
                from .core import approve
                return approve(store, args.run_id, args.plan_hash)
            if args.command == "port":
                from .agent import port_with_agent
                from .github_runner import run_build
                if state.get("interruptedPort"):
                    require_approval(store, state, "interrupted-port")
                elif state.get("workingSourceHash"):
                    if not state.get("buildFailure"):
                        raise WorkbenchError("A source patch already exists. Approve/build it instead of generating another unrequested patch.")
                    require_approval(store, state, "build-patch")
                else:
                    require_approval(store, state, "source-plan")
                if not state.get("baseline", {}).get("passed"):
                    state = run_build(store, state, config)
                begin_port(store, state)
                asyncio.run(port_with_agent(store, state, config))
                return finalize_port(store, state)
            if state.get("interruptedPort"):
                raise WorkbenchError("Source editing was interrupted. Approve and resume porting before building or claiming evidence.")
            if args.command == "build":
                from .github_runner import run_build
                return run_build(store, state, config, retry_dispatch=args.retry_dispatch)
            if args.command == "verify":
                from .evidence import verify_package
                return verify_package(store, state, config)
            if args.command == "inject-fault":
                from .evidence import inject_fault
                return inject_fault(store, state)
            if args.command == "repair":
                from .evidence import repair_package
                return asyncio.run(repair_package(store, state, config))
            if args.command == "record-device":
                from .evidence import record_device_evidence
                return record_device_evidence(store, state, args.report, config)
            if args.command == "export":
                from .evidence import export_evidence
                return export_evidence(store, state)
            raise WorkbenchError("Unsupported mutation command.")
        except (WorkbenchError, OSError, ValueError, KeyError, subprocess.SubprocessError, TimeoutError) as error:
            if args.command == "port" and state.get("portAttempt", {}).get("status") == "running":
                capture_interrupted_port(store, state, f"Source-editing attempt failed: {error}")
            else:
                store.event(state, state["stage"], "failed", str(error))
            raise WorkbenchError(str(error), state) from error


def main() -> int:
    parser = argparse.ArgumentParser(prog="repo-to-arm")
    parser.add_argument("--root", required=True, type=pathlib.Path)
    commands = parser.add_subparsers(dest="command", required=True)
    configure = commands.add_parser("configure")
    configure.add_argument("--runner-repository", required=True)
    configure.add_argument("--runner-ref", required=True, help="Protected refs/tags/<tag> with no update/deletion bypass.")
    configure.add_argument("--runner-ruleset-id", required=True, type=int)
    configure.add_argument("--runner-gh-config-dir")
    configure.add_argument("--copilot-gh-config-dir")
    configure.add_argument("--copilot-user")
    configure.add_argument("--gh-path", default="gh")
    configure.add_argument("--pwsh-path", default="pwsh")
    configure.add_argument("--model", default="gpt-6-astra")
    scout = commands.add_parser("scout")
    scout.add_argument("repository")
    scout.add_argument("--project")
    scout.add_argument("--commit")
    scout.add_argument("--release-repo", action="append", default=[],
                       help="Explicit related release repository; repeat to include separate development channels.")
    for name in ("status", "port", "build", "verify", "inject-fault", "repair", "export"):
        command = commands.add_parser(name)
        command.add_argument("run_id")
        if name == "build":
            command.add_argument("--retry-dispatch", action="store_true",
                                 help="Explicitly resend an unconfirmed dispatch only when GitHub has no matching run.")
    approval = commands.add_parser("approve")
    approval.add_argument("run_id")
    approval.add_argument("--plan-hash", required=True)
    evidence = commands.add_parser("record-device")
    evidence.add_argument("run_id")
    evidence.add_argument("--report", required=True, type=pathlib.Path)
    args = parser.parse_args()
    state = None
    try:
        store = Store(args.root, emit=True)
        if args.command == "configure":
            config = {
                "schemaVersion": 1, "runnerRepository": parse_repository(args.runner_repository),
                "runnerRef": args.runner_ref, "ghPath": args.gh_path, "pwshPath": args.pwsh_path,
                "runnerRulesetId": args.runner_ruleset_id, "model": args.model,
            }
            for arg, key in (("runner_gh_config_dir", "runnerGhConfigDir"),
                             ("copilot_gh_config_dir", "copilotGhConfigDir"),
                             ("copilot_user", "copilotUser")):
                value = getattr(args, arg)
                if value:
                    config[key] = value
            from .github_runner import pinned_runner
            pinned_runner(config)
            write_json(store.root / "config.json", config)
            print(json.dumps({"kind": "configured", "root": str(store.root)}))
            return 0
        if args.command == "scout":
            from .github_runner import bind_runner
            config = configuration(store)
            state = prepare_source(store, args.repository, args.project, args.commit, args.release_repo)
            with store.lock(state["id"]):
                state = store.load(state["id"])
                try:
                    state = bind_runner(store, state, config)
                    store.event(state, "approval", "needs-approval", "Source, runner commit, and exact commands are pinned. Approve this plan hash to continue.")
                except (WorkbenchError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
                    store.event(state, "approval", "failed", str(error))
                    raise WorkbenchError(str(error), state) from error
        else:
            if args.command == "status":
                state = store.load(args.run_id)
                print(json.dumps({"kind": "result", "run": state}, ensure_ascii=False))
                return 0
            state = mutate(store, args)
        print(json.dumps({"kind": "result", "run": state}, ensure_ascii=False), flush=True)
        return 0
    except (WorkbenchError, OSError, ValueError, KeyError, subprocess.SubprocessError, TimeoutError) as error:
        state = getattr(error, "run_state", None) or state
        print(json.dumps({"kind": "error", "error": str(error), "run": state}, ensure_ascii=False), flush=True)
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
