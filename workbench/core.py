from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile
from typing import Any, Iterator


TOOLKIT = pathlib.Path(__file__).resolve().parents[1]
STAGES = (
    ("scout", "Scout"),
    ("approval", "Approve plan"),
    ("baseline", "Unchanged baseline"),
    ("port", "Port"),
    ("build", "Isolated build"),
    ("verify", "Verify"),
    ("evidence", "Evidence"),
)
REPO_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
RUN_PATTERN = re.compile(r"[0-9a-f]{12}")


class WorkbenchError(RuntimeError):
    def __init__(self, message: str, run_state: dict[str, Any] | None = None):
        super().__init__(message)
        self.run_state = run_state


def now() -> str:
    return dt.datetime.now().astimezone().isoformat()


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def file_hash(path: pathlib.Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path: pathlib.Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkbenchError(f"Cannot read JSON at {path}: {error}") from error


def parse_repository(value: str) -> str:
    if value.startswith("https://"):
        parsed = urllib.parse.urlparse(value)
        if parsed.hostname != "github.com" or parsed.query or parsed.fragment or parsed.username or parsed.port:
            raise WorkbenchError("Use a public github.com owner/repository URL.")
        value = parsed.path.strip("/")
    if value.endswith(".git"):
        value = value[:-4]
    if not REPO_PATTERN.fullmatch(value) or any(part in {".", ".."} for part in value.split("/")):
        raise WorkbenchError("Repository must be owner/name, without extra URL paths or shell syntax.")
    return value


def relative_path(value: str) -> pathlib.PurePosixPath:
    value = value.replace("\\", "/")
    candidate = pathlib.PurePosixPath(value)
    if (
        not value or candidate.is_absolute() or ":" in value or "\x00" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(part.lower() == ".git" for part in candidate.parts)
        or any(part.endswith((" ", ".")) or pathlib.PureWindowsPath(part).is_reserved() for part in candidate.parts)
    ):
        raise WorkbenchError(f"Unsafe repository-relative path: {value!r}")
    return candidate


def below(root: pathlib.Path, value: str, *, must_exist: bool = False) -> pathlib.Path:
    relative = relative_path(value)
    root = root.resolve()
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise WorkbenchError(f"Symbolic links are not allowed: {current}")
        if current.exists() and getattr(current.stat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise WorkbenchError(f"Reparse points are not allowed: {current}")
    resolved = current.resolve()
    if not resolved.is_relative_to(root):
        raise WorkbenchError(f"Path escaped its trusted root: {value}")
    if must_exist and not resolved.is_file():
        raise WorkbenchError(f"Expected file not found: {value}")
    return resolved


def tree_manifest(root: pathlib.Path) -> list[dict[str, Any]]:
    result = []
    for path in sorted(root.rglob("*")):
        if ".git" in path.relative_to(root).parts:
            continue
        if path.is_symlink() or (path.exists() and getattr(path.stat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise WorkbenchError(f"Source contains a link/reparse point: {path}")
        if path.is_file():
            result.append({"path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size, "sha256": file_hash(path)})
    return result


def tree_hash(root: pathlib.Path) -> str:
    return digest(tree_manifest(root))


def safe_git_environment(home: pathlib.Path) -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True)
    empty = home / "empty-gitconfig"
    if not empty.exists():
        empty.write_text("", encoding="utf-8")
    return {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(empty),
        "GIT_ATTR_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
    }


def git(arguments: list[str], *, cwd: pathlib.Path | None = None, home: pathlib.Path, timeout: int = 300,
        preserve_output: bool = False, accepted_codes: tuple[int, ...] = (0,)) -> str:
    hooks = home / "no-hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    command = ["git", "-c", f"core.hooksPath={hooks}", "-c", "core.autocrlf=false", "-c", "core.symlinks=false", *arguments]
    completed = subprocess.run(command, cwd=cwd, env=safe_git_environment(home), capture_output=True, timeout=timeout)
    if completed.returncode not in accepted_codes:
        raise WorkbenchError(f"Git failed ({completed.returncode}): {completed.stderr.decode('utf-8', errors='replace').strip()}")
    output = completed.stdout.decode("utf-8")
    return output if preserve_output else output.strip()


def github_json(endpoint: str) -> Any:
    if not endpoint.startswith("/repos/"):
        raise WorkbenchError("Only public repository metadata is accepted.")
    request = urllib.request.Request("https://api.github.com" + endpoint, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "RepoToArm-Workbench",
    })
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.load(response)


def discover_projects(source: pathlib.Path) -> list[dict[str, Any]]:
    projects = []
    for file in sorted(source.rglob("*.csproj")):
        rel = file.relative_to(source).as_posix()
        if any(part in {".git", "obj", "bin"} for part in file.relative_to(source).parts):
            continue
        if file.stat().st_size > 1024 * 1024:
            raise WorkbenchError(f"Project file is too large to inspect: {rel}")
        try:
            root = ET.parse(file).getroot()
        except ET.ParseError as error:
            raise WorkbenchError(f"Cannot inspect {rel}: {error}") from error
        properties: dict[str, list[str]] = {}
        ancestors = []
        parent = file.parent
        while parent.is_relative_to(source):
            inherited = parent / "Directory.Build.props"
            if inherited.exists():
                try:
                    ancestors.append(ET.parse(inherited).getroot())
                except ET.ParseError as error:
                    raise WorkbenchError(f"Cannot inspect inherited properties at {inherited}: {error}") from error
            if parent == source:
                break
            parent = parent.parent
        for definition in [*reversed(ancestors), root]:
            for item in definition.iter():
                if item.text and item.text.strip():
                    properties.setdefault(item.tag.rsplit("}", 1)[-1], []).append(item.text.strip())
        frameworks = ";".join(dict.fromkeys(properties.get("TargetFramework", []) + properties.get("TargetFrameworks", [])))
        output_types = properties.get("OutputType", [])
        windows_ui = "true" in [v.lower() for key in ("UseWPF", "UseWindowsForms") for v in properties.get(key, [])]
        executable = any(value.lower() in {"winexe", "exe"} for value in output_types)
        projects.append({
            "path": rel, "sdkStyle": bool(root.attrib.get("Sdk")),
            "frameworks": frameworks, "windowsDesktop": windows_ui,
            "executable": executable, "assemblyName": (properties.get("AssemblyName") or [file.stem])[-1],
            "platformTargets": properties.get("PlatformTarget", []),
            "isTestProject": any(value.lower() == "true" for value in properties.get("IsTestProject", []))
                or any("test" in item.attrib.get("Include", "").lower() for item in root.iter()
                       if item.tag.rsplit("}", 1)[-1] == "PackageReference"),
        })
    return projects


def choose_project(projects: list[dict[str, Any]], requested: str | None) -> dict[str, Any]:
    candidates = [p for p in projects if p["sdkStyle"] and p["windowsDesktop"] and p["executable"]]
    if requested:
        requested = relative_path(requested).as_posix()
        candidates = [p for p in candidates if p["path"] == requested]
    if len(candidates) != 1:
        raise WorkbenchError("Choose one SDK-style Windows desktop executable project: " +
                             ", ".join(p["path"] for p in projects if p["executable"]))
    project = candidates[0]
    assembly = project["assemblyName"]
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_. -]{0,120}", assembly) or pathlib.PureWindowsPath(assembly).is_reserved():
        raise WorkbenchError("This prototype requires a literal, safe application assembly name.")
    frameworks = project["frameworks"].split(";")
    supported = [f for f in frameworks if re.fullmatch(r"net(?:8|9|10)\.0-windows(?:\d+\.\d+\.\d+\.\d+)?", f)]
    if len(supported) != 1:
        raise WorkbenchError("This prototype supports one .NET 8/9/10 Windows target framework; narrow the project explicitly.")
    return {**project, "framework": supported[0]}


def sdk_for(source: pathlib.Path, framework: str) -> str:
    global_json = source / "global.json"
    if global_json.exists():
        version = read_json(global_json).get("sdk", {}).get("version")
        if not isinstance(version, str) or not re.fullmatch(r"(?:8|9|10)\.\d+\.\d{3}", version):
            raise WorkbenchError("The repository's global.json must name an exact supported .NET SDK.")
        return version
    return "10.0.401" if framework.startswith("net10.") else "9.0.318"


def commands_for(project: str, framework: str, runtime: str, output: str) -> list[list[str]]:
    if runtime not in {"win-x64", "win-arm64"}:
        raise WorkbenchError("Unsupported runtime.")
    relative_path(project)
    return [
        ["dotnet", "restore", project, "--runtime", runtime, "--nologo"],
        ["dotnet", "publish", project, "--configuration", "Release", "--framework", framework,
         "--runtime", runtime, "--self-contained", "true", "--output", output,
         "-p:PublishReadyToRun=false", "-p:PublishTrimmed=false", "--nologo"],
    ]


def test_commands_for(projects: list[dict[str, Any]], output: str) -> list[list[str]]:
    return [
        ["dotnet", "test", project["path"], "-c", "Release", "--logger", "trx", "--results-directory", output]
        for project in projects if project["isTestProject"]
    ]


def collect_scout(store: Store, state: dict[str, Any], source: pathlib.Path, commit: str) -> pathlib.Path:
    run = store.path(state["id"])
    output = run / "assessment"
    completed = subprocess.run([
        sys.executable, str(TOOLKIT / "scripts" / "woa_scout.py"), "--repo", state["repository"],
        "--local-path", str(source), "--output", str(output),
    ], capture_output=True, text=True, encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"}, timeout=600)
    (run / "scout.log").write_text(completed.stdout + "\n" + completed.stderr, encoding="utf-8")
    if completed.returncode:
        raise WorkbenchError("Scout assessment failed. See scout.log; no build plan is approved.")
    path = output / "assessment.json"
    report = read_json(path)
    identity = report.get("repository", {})
    if (identity.get("fullName") != state["repository"] or identity.get("analyzedCommit") != commit
            or identity.get("workingTreeDirty") is not False):
        raise WorkbenchError("Scout did not assess the exact clean source commit.")
    state["artifacts"].update({"assessment": str(path), "scoutReport": str(output / "assessment.md")})
    return path


class Store:
    def __init__(self, root: pathlib.Path, emit: bool = False):
        self.root = root.expanduser().resolve()
        if len(self.root.parts) < 3 or self.root == pathlib.Path.home():
            raise WorkbenchError("Choose a dedicated run directory, not a drive or home root.")
        if any(part.lower().startswith("onedrive") for part in self.root.parts):
            raise WorkbenchError("Source, builds, and run scratch must stay outside OneDrive.")
        self.root.mkdir(parents=True, exist_ok=True)
        self.emit = emit
        self._mutex = threading.RLock()

    def path(self, run_id: str) -> pathlib.Path:
        if not RUN_PATTERN.fullmatch(run_id):
            raise WorkbenchError("Invalid run identifier.")
        path = self.root / run_id
        if path.is_symlink() or not path.resolve().is_relative_to(self.root):
            raise WorkbenchError("Run directory is not trusted.")
        return path

    def load(self, run_id: str) -> dict[str, Any]:
        result = read_json(self.path(run_id) / "run.json")
        if result.get("id") != run_id or result.get("schemaVersion") != 1:
            raise WorkbenchError("Run identity/schema mismatch.")
        return result

    def save(self, state: dict[str, Any]) -> None:
        with self._mutex:
            state["updatedAt"] = now()
            write_json(self.path(state["id"]) / "run.json", state)

    def event(self, state: dict[str, Any], stage: str, status: str, message: str, **data: Any) -> None:
        with self._mutex:
            matching = [item for item in state["stages"] if item["id"] == stage]
            if len(matching) != 1:
                raise WorkbenchError(f"Unknown stage: {stage}")
            matching[0].update({"status": status, "detail": message})
            state["stage"], state["status"] = stage, status
            state["eventSequence"] = state.get("eventSequence", 0) + 1
            event = {"kind": "event", "at": now(), "runId": state["id"], "stage": stage,
                     "sequence": state["eventSequence"], "status": status, "message": message, **data}
            with (self.path(state["id"]) / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            self.save(state)
            if self.emit:
                print(json.dumps(event, ensure_ascii=False), flush=True)

    @contextlib.contextmanager
    def lock(self, run_id: str) -> Iterator[None]:
        path = self.path(run_id) / ".operation.lock"
        with path.open("a+b") as stream:
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise WorkbenchError("Another operation owns this run. Do not execute concurrent mutations.") from error
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def create(self, repository: str) -> dict[str, Any]:
        repository = parse_repository(repository)
        run_id = uuid.uuid4().hex[:12]
        path = self.path(run_id)
        path.mkdir()
        state = {
            "schemaVersion": 1, "id": run_id, "repository": repository,
            "createdAt": now(), "stage": "scout", "status": "pending",
            "stages": [{"id": key, "title": title, "status": "pending", "detail": ""} for key, title in STAGES],
            "artifacts": {"root": str(path)}, "totals": {}, "approvals": [],
        }
        self.save(state)
        return state


def prepare_source(store: Store, repository: str, requested_project: str | None = None,
                   commit: str | None = None) -> dict[str, Any]:
    state = store.create(repository)
    with store.lock(state["id"]):
        return _prepare_source(store, state, requested_project, commit)


def _prepare_source(store: Store, state: dict[str, Any], requested_project: str | None,
                    commit: str | None) -> dict[str, Any]:
    run = store.path(state["id"])
    store.event(state, "scout", "running", "Resolving public repository and pinning source.")
    try:
        metadata = github_json(f"/repos/{state['repository']}")
        if metadata.get("private"):
            raise WorkbenchError("This prototype accepts public repositories only.")
        branch = metadata["default_branch"]
        commit = commit or github_json(f"/repos/{state['repository']}/commits/{urllib.parse.quote(branch, safe='')}")["sha"]
        if not COMMIT_PATTERN.fullmatch(commit):
            raise WorkbenchError("Source must resolve to a full immutable commit SHA.")
        source = run / "source"
        git(["clone", "--no-checkout", "--filter=blob:none", f"https://github.com/{state['repository']}.git", str(source)],
            home=run / "git-home")
        git(["checkout", "--detach", commit], cwd=source, home=run / "git-home")
        actual = git(["rev-parse", "HEAD"], cwd=source, home=run / "git-home")
        if actual != commit:
            raise WorkbenchError("Checked-out source does not match the assessed commit.")
        if (source / ".gitmodules").exists():
            raise WorkbenchError("Submodule projects require a separately reviewed input closure.")
        assessment = collect_scout(store, state, source, commit)
        projects = discover_projects(source)
        project = choose_project(projects, requested_project)
        manifest = tree_manifest(source)
        state.update({"sourceCommit": commit, "project": project["path"], "framework": project["framework"],
                      "assemblyName": project["assemblyName"], "sourceHash": digest(manifest),
                      "projects": projects, "title": metadata.get("description") or state["repository"]})
        write_json(run / "source-manifest.json", manifest)
        plan = {
            "schemaVersion": 1, "runId": state["id"], "repository": state["repository"], "sourceCommit": commit,
            "sourceHash": state["sourceHash"], "project": project["path"], "framework": project["framework"],
            "assemblyName": project["assemblyName"], "sdkVersion": sdk_for(source, project["framework"]),
            "execution": "github-hosted-windows", "architectures": ["win-x64", "win-arm64"],
            "commands": {rid: commands_for(project["path"], project["framework"], rid, f"<isolated-output>/{rid}")
                         for rid in ("win-x64", "win-arm64")},
            "sourceIsolation": "worktree-per-runtime",
            "testCommands": test_commands_for(projects, "<isolated-output>/tests"),
            "assessmentSha256": file_hash(assessment),
            "scope": "Native portable core; no installer/updater rewrite; no arbitrary shell commands.",
        }
        write_json(run / "plan.json", plan)
        state.update({"planHash": digest(plan), "approvalKind": "source-plan", "plan": plan})
        store.event(state, "scout", "passed", "Source pinned and desktop project identified.", sourceCommit=commit)
        store.event(state, "approval", "needs-approval", "Review the exact pinned source and isolated build commands.")
        return state
    except (WorkbenchError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        store.event(state, "scout", "failed", str(error))
        raise WorkbenchError(str(error), state) from error


def approve(store: Store, run_id: str, plan_hash: str) -> dict[str, Any]:
    state = store.load(run_id)
    if state.get("planHash") != plan_hash:
        raise WorkbenchError("Approval hash does not match the current plan.")
    run = store.path(run_id)
    if file_hash(run / "assessment" / "assessment.json") != state["plan"]["assessmentSha256"]:
        raise WorkbenchError("Scout evidence changed after the plan was prepared.")
    if state["approvalKind"] == "source-plan":
        expected = digest(read_json(run / "plan.json"))
        source_hash = tree_hash(run / "source")
        if expected != plan_hash or source_hash != state["sourceHash"]:
            raise WorkbenchError("Source or plan changed after assessment.")
    elif state["approvalKind"] in {"build-patch", "interrupted-port"}:
        name = "build-plan.json" if state["approvalKind"] == "build-patch" else "interrupted-port-plan.json"
        plan = read_json(run / name)
        if digest(plan) != plan_hash or tree_hash(run / "source") != plan["workingSourceHash"]:
            raise WorkbenchError("Edited source changed after plan creation.")
    else:
        raise WorkbenchError("No supported approval is pending.")
    approval = {"kind": state["approvalKind"], "planHash": plan_hash, "approvedAt": now(),
                "channel": "explicit-operator-command"}
    state["approvals"].append(approval)
    write_json(run / f"approval-{state['approvalKind']}.json", approval)
    store.event(state, "approval", "approved", f"Approved immutable {state['approvalKind']} {plan_hash[:12]}.")
    return state


def require_approval(store: Store, state: dict[str, Any], kind: str) -> None:
    plans = {"source-plan": "plan.json", "build-patch": "build-plan.json",
             "interrupted-port": "interrupted-port-plan.json"}
    if kind not in plans:
        raise WorkbenchError("Unsupported approval kind.")
    plan = read_json(store.path(state["id"]) / plans[kind])
    actual = digest(plan)
    if actual != state["planHash"]:
        raise WorkbenchError("This approval is not for the current plan.")
    if file_hash(store.path(state["id"]) / "assessment" / "assessment.json") != plan["assessmentSha256"]:
        raise WorkbenchError("Scout evidence changed after approval.")
    matches = [approval for approval in state["approvals"] if approval["kind"] == kind and approval["planHash"] == actual]
    if not matches:
        raise WorkbenchError(f"Explicit approval is required for the current {kind}.")
    source_hash = plan["sourceHash"] if kind == "source-plan" else plan["workingSourceHash"]
    if tree_hash(store.path(state["id"]) / "source") != source_hash:
        raise WorkbenchError("Source changed after the approval-bound fingerprint.")


def source_diff(store: Store, state: dict[str, Any]) -> str:
    source = store.path(state["id"]) / "source"
    tracked = git(["diff", "--no-ext-diff", "--no-textconv", "--binary", "HEAD"], cwd=source,
                  home=store.path(state["id"]) / "git-home", preserve_output=True)
    untracked = git(["ls-files", "--others", "--exclude-standard"], cwd=source,
                    home=store.path(state["id"]) / "git-home").splitlines()
    additions = []
    for relative in untracked:
        below(source, relative, must_exist=True)
        additions.append(git(["diff", "--no-index", "--no-ext-diff", "--no-textconv", "--", "/dev/null", relative],
                             cwd=source, home=store.path(state["id"]) / "git-home",
                             preserve_output=True, accepted_codes=(0, 1)))
    return tracked + "".join(additions)


def validate_patch_tree(store: Store, state: dict[str, Any], patch_path: pathlib.Path, expected_hash: str) -> pathlib.Path:
    run = store.path(state["id"])
    source = run / "source"
    check = run / "patch-verification" / uuid.uuid4().hex[:8]
    check.parent.mkdir(exist_ok=True)
    git(["worktree", "add", "--detach", str(check), state["sourceCommit"]], cwd=source, home=run / "git-home")
    git(["apply", "--check", str(patch_path)], cwd=check, home=run / "git-home")
    git(["apply", str(patch_path)], cwd=check, home=run / "git-home")
    if tree_hash(check) != expected_hash:
        raise WorkbenchError("Serialized patch does not reproduce the reviewed source tree.")
    return check


def invalidate_package_state(state: dict[str, Any]) -> None:
    for key in ("packages", "activePackage", "candidateBuild", "verification", "deviceEvidence", "fault", "recovery"):
        state.pop(key, None)
    for key in ("arm64Package", "x64Package", "architectureReport", "deviceEvidence", "evidence", "buildReceipt"):
        state["artifacts"].pop(key, None)
    state["totals"] = {}
    for stage in state["stages"]:
        if stage["id"] in {"verify", "evidence"}:
            stage.update({"status": "pending", "detail": "A new candidate requires fresh evidence."})


def begin_port(store: Store, state: dict[str, Any]) -> None:
    invalidate_package_state(state)
    state["portAttempt"] = {"id": uuid.uuid4().hex, "status": "running", "at": now(),
                            "inputPlanHash": state["planHash"]}
    store.event(state, "port", "running", "Starting the scoped source-editing agent; incomplete edits will require approval to resume.")


def capture_interrupted_port(store: Store, state: dict[str, Any], reason: str) -> dict[str, Any]:
    run = store.path(state["id"])
    patch = source_diff(store, state)
    folder = run / "interrupted-ports" / uuid.uuid4().hex[:10]
    folder.mkdir(parents=True)
    patch_path = folder / "partial.patch"
    patch_path.write_bytes(patch.encode("utf-8"))
    plan = {
        **state["plan"], "approvalPurpose": "resume-interrupted-source-edit",
        "workingSourceHash": tree_hash(run / "source"), "patchSha256": file_hash(patch_path),
        "attemptId": state["portAttempt"]["id"],
    }
    write_json(folder / "plan.json", plan)
    write_json(run / "interrupted-port-plan.json", plan)
    invalidate_package_state(state)
    state["portAttempt"]["status"] = "interrupted"
    state["interruptedPort"] = {"reason": reason, "at": now(), "patch": str(patch_path),
                                "workingSourceHash": plan["workingSourceHash"]}
    state.update({"planHash": digest(plan), "approvalKind": "interrupted-port"})
    state["artifacts"]["partialPatch"] = str(patch_path)
    store.event(state, "port", "interrupted", reason)
    store.event(state, "approval", "needs-approval", "Partial edits are preserved, not completed. Review and approve this resume plan.")
    return state


def reconcile_interrupted_port(store: Store, state: dict[str, Any]) -> bool:
    attempt = state.get("portAttempt", {})
    if attempt.get("status") == "running":
        capture_interrupted_port(store, state, "The previous source-editing process ended without completing its port.")
        return True
    partial = state.get("interruptedPort")
    if partial and tree_hash(store.path(state["id"]) / "source") != partial["workingSourceHash"]:
        capture_interrupted_port(store, state, "Partial source changed; the earlier resume approval is no longer valid.")
        return True
    return False


def finalize_port(store: Store, state: dict[str, Any]) -> dict[str, Any]:
    run = store.path(state["id"])
    patch = source_diff(store, state)
    if not patch.strip():
        raise WorkbenchError("The porting agent produced no changes; no port will be claimed.")
    if len(patch.encode("utf-8")) > 50000:
        raise WorkbenchError("This prototype requires a reviewed patch under 50 KB for the isolated runner.")
    (run / "port.patch").write_text(patch, encoding="utf-8", newline="\n")
    state["workingSourceHash"] = tree_hash(run / "source")
    validated = validate_patch_tree(store, state, run / "port.patch", state["workingSourceHash"])
    build_plan = {
        **state["plan"], "patchSha256": hashlib.sha256(patch.encode("utf-8")).hexdigest(),
        "workingSourceHash": state["workingSourceHash"],
        "testCommands": test_commands_for(discover_projects(run / "source"), "<isolated-output>/tests"),
    }
    write_json(run / "build-plan.json", build_plan)
    invalidate_package_state(state)
    state.pop("interruptedPort", None)
    if state.get("portAttempt"):
        state["portAttempt"]["status"] = "completed"
    state["artifacts"].pop("partialPatch", None)
    state.update({"planHash": digest(build_plan), "approvalKind": "build-patch"})
    state["artifacts"]["patch"] = str(run / "port.patch")
    state["artifacts"]["patchVerification"] = str(validated)
    store.event(state, "port", "passed", "Agent changes recorded; source diff must be reviewed before building.")
    store.event(state, "approval", "needs-approval", "Review the source diff and approve the exact build plan.")
    return state


def safe_extract(archive: pathlib.Path, destination: pathlib.Path, *, max_bytes: int = 2_000_000_000) -> None:
    if destination.exists():
        raise WorkbenchError("Artifact extraction requires a fresh directory.")
    with zipfile.ZipFile(archive) as zipped:
        infos = zipped.infolist()
        if not infos or len(infos) > 50000 or sum(item.file_size for item in infos) > max_bytes:
            raise WorkbenchError("Artifact archive is empty or exceeds the supported size/file limits.")
        seen = set()
        for item in infos:
            name = item.filename.rstrip("/")
            relative = relative_path(name)
            key = relative.as_posix().casefold()
            if key in seen:
                raise WorkbenchError("Artifact archive contains duplicate/case-colliding paths.")
            seen.add(key)
            if stat.S_ISLNK(item.external_attr >> 16):
                raise WorkbenchError("Artifact archive contains a symbolic link.")
        destination.mkdir(parents=True)
        for item in infos:
            target = below(destination, item.filename.rstrip("/"))
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zipped.open(item) as incoming, target.open("xb") as outgoing:
                    for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                        outgoing.write(chunk)
