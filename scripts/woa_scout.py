#!/usr/bin/env python3
"""Generate a deterministic Windows on Arm repository readiness assessment."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


API_ROOT = "https://api.github.com"
SKIP_DIRECTORIES = {
    ".git",
    ".vs",
    "bin",
    "obj",
    "node_modules",
    "packages",
    "artifacts",
    "out",
    "dist",
}
TEXT_SUFFIXES = {
    ".cs",
    ".csproj",
    ".cpp",
    ".c",
    ".h",
    ".hpp",
    ".props",
    ".targets",
    ".vcxproj",
    ".sln",
    ".slnx",
    ".json",
    ".yaml",
    ".yml",
    ".ps1",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".rs",
    ".toml",
    ".xml",
    ".wxs",
    ".wixproj",
    ".iss",
    ".nsi",
    ".md",
}
ROOT_TEXT_FILES = {
    "cmakelists.txt",
    "cargo.toml",
    "package.json",
    "pyproject.toml",
    "setup.py",
    "global.json",
    "directory.build.props",
    "directory.build.targets",
}


def parse_repository(value: str) -> tuple[str, str]:
    candidate = value.strip().rstrip("/")
    match = re.search(r"github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?$", candidate)
    if match:
        return match.group(1), match.group(2)
    parts = candidate.split("/")
    if len(parts) == 2 and all(parts):
        return parts[0], parts[1]
    raise ValueError(f"Repository must be owner/repo or a GitHub URL: {value}")


def github_get(path_or_url: str, token: str | None = None) -> Any:
    url = path_or_url if path_or_url.startswith("http") else f"{API_ROOT}{path_or_url}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "windows-on-arm-porting-scout",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API {error.code} for {url}: {body[:500]}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"GitHub request failed for {url}: {error}") from error


def safe_github_get(
    path_or_url: str,
    token: str | None,
    warnings: list[str],
    default: Any,
) -> Any:
    try:
        return github_get(path_or_url, token)
    except RuntimeError as error:
        warnings.append(str(error))
        return default


def scan_local_source(root: pathlib.Path) -> tuple[list[str], dict[str, str]]:
    paths: list[str] = []
    contents: dict[str, str] = {}
    for directory, names, files in os.walk(root):
        names[:] = [name for name in names if name.lower() not in SKIP_DIRECTORIES]
        directory_path = pathlib.Path(directory)
        for filename in files:
            path = directory_path / filename
            relative = path.relative_to(root).as_posix()
            paths.append(relative)
            if path.stat().st_size > 512_000:
                continue
            if path.suffix.lower() in TEXT_SUFFIXES or path.name.lower() in ROOT_TEXT_FILES:
                try:
                    contents[relative] = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
    return sorted(paths), contents


def select_remote_text_paths(tree: list[dict[str, Any]]) -> list[str]:
    candidates: list[tuple[int, str]] = []
    for entry in tree:
        if entry.get("type") != "blob" or int(entry.get("size") or 0) > 512_000:
            continue
        path = str(entry.get("path", ""))
        lower = path.lower()
        suffix = pathlib.PurePosixPath(path).suffix.lower()
        name = pathlib.PurePosixPath(path).name.lower()
        if suffix not in TEXT_SUFFIXES and name not in ROOT_TEXT_FILES:
            continue
        priority = 3
        if name in ROOT_TEXT_FILES or suffix in {".sln", ".slnx", ".csproj", ".vcxproj"}:
            priority = 0
        elif lower.startswith(".github/workflows/") or suffix in {".props", ".targets", ".pubxml"}:
            priority = 1
        elif any(token in lower for token in ("install", "package", "release", "build", "plugin")):
            priority = 2
        candidates.append((priority, path))
    candidates.sort()
    return [path for _, path in candidates[:80]]


def fetch_remote_contents(
    owner: str,
    repository: str,
    branch: str,
    paths: list[str],
    warnings: list[str],
) -> dict[str, str]:
    contents: dict[str, str] = {}
    encoded_branch = urllib.parse.quote(branch, safe="")
    for path in paths:
        encoded_path = "/".join(urllib.parse.quote(part, safe="") for part in path.split("/"))
        url = f"https://raw.githubusercontent.com/{owner}/{repository}/{encoded_branch}/{encoded_path}"
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "windows-on-arm-porting-scout"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                contents[path] = response.read().decode("utf-8", errors="replace")
        except (urllib.error.HTTPError, urllib.error.URLError) as error:
            warnings.append(f"Could not read {path}: {error}")
    return contents


def detect_build_systems(paths: list[str]) -> list[dict[str, Any]]:
    lower_paths = [path.lower() for path in paths]
    definitions = {
        "dotnet": lambda path: path.endswith((".csproj", ".sln", ".slnx")),
        "native-msbuild": lambda path: path.endswith(".vcxproj"),
        "cmake": lambda path: path.endswith("cmakelists.txt"),
        "node": lambda path: path.endswith("package.json"),
        "rust": lambda path: path.endswith("cargo.toml"),
        "python": lambda path: path.endswith(("pyproject.toml", "setup.py")),
    }
    systems = []
    for name, predicate in definitions.items():
        evidence = [paths[index] for index, path in enumerate(lower_paths) if predicate(path)]
        if evidence:
            systems.append({"name": name, "evidence": evidence[:10]})
    return systems


def detect_packaging(paths: list[str], text: str) -> list[dict[str, Any]]:
    lower_paths = [path.lower() for path in paths]
    checks = {
        "msix": any(path.endswith(("package.appxmanifest", ".msix", ".appx")) for path in lower_paths),
        "squirrel": "squirrel" in text,
        "velopack": "velopack" in text or "vpk" in text,
        "wix": any(path.endswith((".wixproj", ".wxs")) for path in lower_paths),
        "inno-setup": any(path.endswith(".iss") for path in lower_paths),
        "nsis": any(path.endswith(".nsi") for path in lower_paths),
        "electron-builder": "electron-builder" in text,
        "github-actions": any(path.startswith(".github/workflows/") for path in lower_paths),
        "appveyor": any(path.endswith("appveyor.yml") for path in lower_paths),
        "azure-pipelines": any(path.endswith("azure-pipelines.yml") for path in lower_paths),
    }
    return [{"name": name} for name, present in checks.items() if present]


def extract_dependencies(contents: dict[str, str]) -> list[str]:
    dependencies: set[str] = set()
    package_reference = re.compile(r"<PackageReference\s+Include=[\"']([^\"']+)", re.I)
    npm_dependency = re.compile(r"[\"']([^\"']+)[\"']\s*:\s*[\"'][~^<>=*\d]", re.I)
    for path, content in contents.items():
        lower = path.lower()
        if lower.endswith((".csproj", ".props", ".targets")):
            dependencies.update(package_reference.findall(content))
        elif lower.endswith("package.json"):
            dependencies.update(npm_dependency.findall(content))
    return sorted(dependencies)


def detect_tests(paths: list[str]) -> bool:
    for path in paths:
        normalized = path.replace("\\", "/").lower()
        parts = pathlib.PurePosixPath(normalized).parts
        filename = pathlib.PurePosixPath(normalized).name
        if any(part in {"test", "tests"} for part in parts):
            return True
        if re.search(r"(^|[._-])tests?([._-]|$)", filename):
            return True
    return False


def release_summary(releases: list[dict[str, Any]]) -> dict[str, Any]:
    if not releases:
        return {
            "latest": None,
            "totalAssetDownloads": 0,
            "arm64Assets": [],
            "x64Assets": [],
            "windowsAssets": [],
        }
    latest = next(
        (release for release in releases if not release.get("draft") and not release.get("prerelease")),
        releases[0],
    )
    assets = list(latest.get("assets") or [])
    arm_pattern = re.compile(r"(arm64|aarch64|windows[-_. ]?on[-_. ]?arm)", re.I)
    x64_pattern = re.compile(r"(^|[-_. ])(x64|amd64|win64)([-_. ]|$)", re.I)
    windows_pattern = re.compile(r"\.(exe|msi|msix|appx|zip|7z|nupkg)$", re.I)
    source_pattern = re.compile(r"(^|[-_. ])source([-_. ]|$)", re.I)
    return {
        "latest": {
            "tag": latest.get("tag_name"),
            "publishedAt": latest.get("published_at"),
            "url": latest.get("html_url"),
            "assets": [
                {
                    "name": asset.get("name"),
                    "size": asset.get("size"),
                    "downloads": asset.get("download_count"),
                    "url": asset.get("browser_download_url"),
                }
                for asset in assets
            ],
        },
        "totalAssetDownloads": sum(int(asset.get("download_count") or 0) for asset in assets),
        "arm64Assets": [asset.get("name") for asset in assets if arm_pattern.search(str(asset.get("name")))],
        "x64Assets": [asset.get("name") for asset in assets if x64_pattern.search(str(asset.get("name")))],
        "windowsAssets": [
            asset.get("name")
            for asset in assets
            if windows_pattern.search(str(asset.get("name")))
            and not source_pattern.search(str(asset.get("name")))
        ],
    }


def score_impact(stars: int, downloads: int) -> int:
    star_component = min(7.0, math.log10(max(stars, 1) + 1) * 1.8)
    download_component = min(3.0, math.log10(max(downloads, 1) + 1) * 0.6)
    return max(1, min(10, round(star_component + download_component)))


def build_assessment(
    metadata: dict[str, Any],
    releases: list[dict[str, Any]],
    paths: list[str],
    contents: dict[str, str],
    issues: list[dict[str, Any]],
    pull_requests: list[dict[str, Any]],
    source_mode: str,
    warnings: list[str],
) -> dict[str, Any]:
    combined_text = "\n".join(contents.values()).lower()
    combined_paths = "\n".join(paths).lower()
    searchable = f"{combined_paths}\n{combined_text}"
    release = release_summary(releases)
    build_systems = detect_build_systems(paths)
    packaging = detect_packaging(paths, combined_text)
    dependencies = extract_dependencies(contents)
    binary_paths = [
        path
        for path in paths
        if pathlib.PurePosixPath(path).suffix.lower() in {".dll", ".exe", ".node", ".lib"}
    ]
    x64_references = len(re.findall(r"\b(win-x64|x64|amd64)\b", searchable))
    arm64_references = len(re.findall(r"\b(win-arm64|arm64|aarch64|arm64ec)\b", searchable))
    language = str(metadata.get("language") or "")
    native_language = language.lower() in {"c", "c++", "rust"} or any(
        path.lower().endswith((".cpp", ".c", ".vcxproj", "cargo.toml")) for path in paths
    )
    plugin_surface = any(
        any(part.lower().startswith("plugin") for part in pathlib.PurePosixPath(path).parts)
        for path in paths
    )
    tests_present = detect_tests(paths)
    packaging_names = {item["name"] for item in packaging}
    arm_gap = bool(release["windowsAssets"] and not release["arm64Assets"])

    risks: list[dict[str, str]] = []
    risk_score = 0
    if native_language:
        risk_score += 2
        risks.append(
            {
                "id": "native-code",
                "severity": "high",
                "evidence": "Repository contains native C, C++, Rust, or VCXPROJ build surfaces.",
            }
        )
    if binary_paths:
        binary_risk = 2 if native_language else 1
        risk_score += binary_risk
        risks.append(
            {
                "id": "committed-binaries",
                "severity": "high" if binary_risk == 2 else "medium",
                "evidence": f"{len(binary_paths)} committed EXE, DLL, LIB, or Node addon paths require architecture verification.",
            }
        )
    if x64_references > arm64_references:
        risk_score += 2
        risks.append(
            {
                "id": "x64-assumptions",
                "severity": "high",
                "evidence": f"Detected {x64_references} x64 references versus {arm64_references} Arm references.",
            }
        )
    if packaging_names.intersection({"squirrel", "wix", "inno-setup", "nsis", "electron-builder"}):
        risk_score += 2
        risks.append(
            {
                "id": "installer-updater",
                "severity": "medium",
                "evidence": "Installer or updater tooling has architecture-specific behavior.",
            }
        )
    if plugin_surface:
        risk_score += 1
        risks.append(
            {
                "id": "plugin-boundary",
                "severity": "medium",
                "evidence": "Plugin loading requires in-process and out-of-process architecture classification.",
            }
        )
    if arm_gap:
        risk_score += 1
        risks.append(
            {
                "id": "release-gap",
                "severity": "medium",
                "evidence": "Latest stable release has x64 assets and no ARM64-labeled asset.",
            }
        )
    if not tests_present:
        risk_score += 1
        risks.append(
            {
                "id": "test-coverage",
                "severity": "medium",
                "evidence": "No obvious test directory or test project was detected.",
            }
        )
    risk_score = min(10, risk_score)

    managed_bonus = 1 if any(system["name"] == "dotnet" for system in build_systems) and not native_language else 0
    arm_evidence_bonus = 1 if arm64_references > 0 or issues or pull_requests else 0
    feasibility_score = max(1, min(10, 10 - risk_score + managed_bonus + arm_evidence_bonus))
    downloads = int(release["totalAssetDownloads"])
    stars = int(metadata.get("stargazers_count") or 0)
    impact_score = score_impact(stars, downloads)
    if feasibility_score >= 7:
        recommendation = "Strong one-week candidate for a native ARM64 proof with portable packaging."
    elif feasibility_score >= 5:
        recommendation = "Feasible in one week only with an explicit subsystem or installer fallback."
    else:
        recommendation = "High-risk one-week port; constrain scope to architecture proof and one core scenario."

    return {
        "schemaVersion": 1,
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "sourceMode": source_mode,
        "repository": {
            "owner": metadata.get("owner", {}).get("login"),
            "name": metadata.get("name"),
            "fullName": metadata.get("full_name"),
            "url": metadata.get("html_url"),
            "defaultBranch": metadata.get("default_branch"),
            "analyzedCommit": metadata.get("_analyzed_commit"),
            "workingTreeDirty": bool(metadata.get("_working_tree_dirty")),
            "workingTreeStatus": metadata.get("_working_tree_status") or [],
            "pushedAt": metadata.get("pushed_at"),
            "license": (metadata.get("license") or {}).get("spdx_id"),
            "primaryLanguage": metadata.get("language"),
            "stars": stars,
            "forks": int(metadata.get("forks_count") or 0),
            "openIssues": int(metadata.get("open_issues_count") or 0),
        },
        "release": release,
        "architecture": {
            "latestReleaseHasArm64Asset": bool(release["arm64Assets"]),
            "latestReleaseHasX64Asset": bool(release["x64Assets"]),
            "latestReleaseHasWindowsAsset": bool(release["windowsAssets"]),
            "releaseAssetGap": arm_gap,
            "x64ReferenceCount": x64_references,
            "arm64ReferenceCount": arm64_references,
            "committedBinaryCount": len(binary_paths),
            "committedBinaryExamples": binary_paths[:20],
            "nativeLanguageDetected": native_language,
        },
        "buildSystems": build_systems,
        "packaging": packaging,
        "dependencies": dependencies,
        "pluginSurfaceDetected": plugin_surface,
        "testsDetected": tests_present,
        "armIssues": issues,
        "armPullRequests": pull_requests,
        "risks": risks,
        "scores": {
            "customerImpact": impact_score,
            "technicalRisk": risk_score,
            "oneWeekFeasibility": feasibility_score,
        },
        "oneWeekFeasible": feasibility_score >= 5,
        "recommendation": recommendation,
        "committedOutcome": [
            "Architecture-isolated x64 and ARM64 builds",
            "ARM64 portable artifact",
            "PE architecture inventory",
            "Physical-device core scenario validation",
            "Before-and-after evidence",
        ],
        "fallback": [
            "Use an operating-system service instead of an unavailable x64-only optional dependency.",
            "Defer installer or updater integration before reducing native core quality.",
            "Report incompatible optional plugins instead of loading them in-process.",
        ],
        "warnings": warnings,
    }


def write_markdown(report: dict[str, Any], path: pathlib.Path) -> None:
    repository = report["repository"]
    scores = report["scores"]
    lines = [
        f"# Windows on Arm Assessment: {repository['fullName']}",
        "",
        f"- Generated: {report['generatedAt']}",
        f"- Source mode: {report['sourceMode']}",
        f"- Default branch: `{repository['defaultBranch']}`",
        f"- Stars: {repository['stars']:,}",
        f"- Latest stable release: `{(report['release']['latest'] or {}).get('tag')}`",
        "",
        "## Decision",
        "",
        report["recommendation"],
        "",
        "| Score | Value |",
        "|---|---:|",
        f"| Customer impact | {scores['customerImpact']}/10 |",
        f"| Technical risk | {scores['technicalRisk']}/10 |",
        f"| One-week feasibility | {scores['oneWeekFeasibility']}/10 |",
        "",
        "## Architecture Gap",
        "",
        f"- Latest release has x64 asset: {report['architecture']['latestReleaseHasX64Asset']}",
        f"- Latest release has Windows binary asset: {report['architecture']['latestReleaseHasWindowsAsset']}",
        f"- Latest release has ARM64 asset: {report['architecture']['latestReleaseHasArm64Asset']}",
        f"- Source x64 references: {report['architecture']['x64ReferenceCount']}",
        f"- Source Arm references: {report['architecture']['arm64ReferenceCount']}",
        f"- Committed native binary candidates: {report['architecture']['committedBinaryCount']}",
        "",
        "## Build and Packaging",
        "",
        "- Build systems: " + ", ".join(item["name"] for item in report["buildSystems"]),
        "- Packaging: " + ", ".join(item["name"] for item in report["packaging"]),
        f"- Plugin surface: {report['pluginSurfaceDetected']}",
        f"- Tests detected: {report['testsDetected']}",
        "",
        "## Risks",
        "",
    ]
    for risk in report["risks"]:
        lines.append(f"- **{risk['severity'].upper()} {risk['id']}:** {risk['evidence']}")
    lines.extend(
        [
            "",
            "## Committed Outcome",
            "",
            *[f"- {item}" for item in report["committedOutcome"]],
            "",
            "## Fallback",
            "",
            *[f"- {item}" for item in report["fallback"]],
        ]
    )
    if report["warnings"]:
        lines.extend(["", "## Collection Warnings", "", *[f"- {item}" for item in report["warnings"]]])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="GitHub owner/repo or repository URL")
    parser.add_argument("--local-path", help="Optional local clone for complete source scanning")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow a dirty local tree and record its status. Clean trees are required by default.",
    )
    parser.add_argument("--output", required=True, help="Output directory")
    parser.add_argument("--github-token-env", default="GITHUB_TOKEN")
    args = parser.parse_args()

    owner, repository = parse_repository(args.repo)
    token = os.environ.get(args.github_token_env)
    warnings: list[str] = []
    metadata = github_get(f"/repos/{owner}/{repository}", token)
    metadata["_working_tree_dirty"] = False
    metadata["_working_tree_status"] = []
    branch = str(metadata["default_branch"])
    commit = safe_github_get(
        f"/repos/{owner}/{repository}/commits/{urllib.parse.quote(branch, safe='')}",
        token,
        warnings,
        {},
    )
    metadata["_analyzed_commit"] = commit.get("sha")
    releases = safe_github_get(
        f"/repos/{owner}/{repository}/releases?per_page=10",
        token,
        warnings,
        [],
    )
    issue_query = urllib.parse.urlencode(
        {"q": f'repo:{owner}/{repository} is:issue (arm64 OR aarch64 OR "windows on arm" OR win-arm64)'}
    )
    pull_query = urllib.parse.urlencode(
        {"q": f'repo:{owner}/{repository} is:pr (arm64 OR aarch64 OR "windows on arm" OR win-arm64)'}
    )
    issue_result = safe_github_get(f"/search/issues?{issue_query}&per_page=30", token, warnings, {"items": []})
    pull_result = safe_github_get(f"/search/issues?{pull_query}&per_page=30", token, warnings, {"items": []})
    issues = [
        {
            "number": item.get("number"),
            "title": item.get("title"),
            "state": item.get("state"),
            "updatedAt": item.get("updated_at"),
            "url": item.get("html_url"),
        }
        for item in issue_result.get("items", [])
    ]
    pull_requests = [
        {
            "number": item.get("number"),
            "title": item.get("title"),
            "state": item.get("state"),
            "updatedAt": item.get("updated_at"),
            "url": item.get("html_url"),
        }
        for item in pull_result.get("items", [])
    ]

    if args.local_path:
        local_root = pathlib.Path(args.local_path).resolve()
        if not local_root.is_dir():
            raise ValueError(f"Local path does not exist: {local_root}")
        paths, contents = scan_local_source(local_root)
        local_commit = subprocess.run(
            ["git", "-C", str(local_root), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
        if local_commit.returncode == 0:
            metadata["_analyzed_commit"] = local_commit.stdout.strip()
        else:
            warnings.append(f"Could not resolve local Git commit: {local_commit.stderr.strip()}")
        local_status = subprocess.run(
            ["git", "-C", str(local_root), "status", "--porcelain=v1", "--untracked-files=all"],
            check=False,
            capture_output=True,
            text=True,
        )
        if local_status.returncode != 0:
            raise ValueError(f"Could not read local Git status: {local_status.stderr.strip()}")
        status_lines = [line for line in local_status.stdout.splitlines() if line]
        metadata["_working_tree_dirty"] = bool(status_lines)
        metadata["_working_tree_status"] = status_lines
        if status_lines and not args.allow_dirty:
            raise ValueError(
                "Local repository is dirty. Commit or clean the assessed tree, or pass --allow-dirty to record the state."
            )
        source_mode = "local"
    else:
        encoded_branch = urllib.parse.quote(branch, safe="")
        tree_response = github_get(
            f"/repos/{owner}/{repository}/git/trees/{encoded_branch}?recursive=1",
            token,
        )
        tree = list(tree_response.get("tree") or [])
        paths = sorted(str(entry.get("path")) for entry in tree if entry.get("type") == "blob")
        selected = select_remote_text_paths(tree)
        contents = fetch_remote_contents(owner, repository, branch, selected, warnings)
        source_mode = "github"

    report = build_assessment(
        metadata,
        releases,
        paths,
        contents,
        issues,
        pull_requests,
        source_mode,
        warnings,
    )
    output = pathlib.Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    (output / "assessment.json").write_text(
        json.dumps(report, indent=2, sort_keys=False),
        encoding="utf-8",
    )
    write_markdown(report, output / "assessment.md")
    print(
        json.dumps(
            {
                "repository": report["repository"]["fullName"],
                "scores": report["scores"],
                "oneWeekFeasible": report["oneWeekFeasible"],
                "output": str(output),
                "warnings": len(report["warnings"]),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, OSError) as error:
        print(f"woa-scout: {error}", file=sys.stderr)
        raise SystemExit(1)
