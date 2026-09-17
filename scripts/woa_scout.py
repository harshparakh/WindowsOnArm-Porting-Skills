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


def classify_release_asset(name: str) -> dict[str, str]:
    """Classify filename evidence only; labels do not verify binary contents."""
    lower = name.lower()

    def labeled(pattern: str) -> bool:
        return bool(re.search(rf"(^|[-_. ])(?:{pattern})(?=[-_. ]|$)", lower))

    platforms: set[str] = set()
    windows_suffixes = (".exe", ".msi", ".msix", ".appx", ".msixbundle", ".appxbundle", ".dll")
    if labeled(r"windows|win(?:32|64)?") or lower.endswith(windows_suffixes):
        platforms.add("windows")
    if labeled(r"macos|mac|osx|darwin") or lower.endswith((".dmg", ".pkg")):
        platforms.add("macos")
    if labeled(r"linux|ubuntu|debian") or lower.endswith((".deb", ".rpm", ".appimage")):
        platforms.add("linux")
    if labeled(r"android|freebsd|openbsd|netbsd|ios"):
        platforms.add("other")
    platform = next(iter(platforms)) if len(platforms) == 1 else "unknown"

    architectures = [
        architecture
        for architecture, pattern in (
            ("arm64", r"arm64|aarch64"),
            ("arm64ec", r"arm64ec"),
            ("x64", r"x64|amd64|x86_64|win64"),
            ("x86", r"x86(?!_64)|i[3-6]86"),
        )
        if labeled(pattern)
    ]
    architecture = architectures[0] if len(architectures) == 1 else "unknown"
    if labeled(r"checksums?|(?:sha(?:1|224|256|384|512)|md5)(?:sums?)?|signatures?") or lower.endswith(
        (".asc", ".sig", ".minisig")
    ):
        kind = "checksum"
    elif labeled(r"source|sources|src"):
        kind = "source"
    elif lower.endswith(
        windows_suffixes
        + (".dmg", ".pkg", ".deb", ".rpm", ".appimage", ".zip", ".7z",
           ".tar.gz", ".tgz", ".tar.xz", ".tar.bz2", ".tar.zst", ".nupkg")
    ) and platform != "unknown" and not labeled(r"symbols?|debugsymbols?|pdb|docs?"):
        kind = "binary"
    else:
        kind = "unknown"
    return {"platform": platform, "architecture": architecture, "kind": kind}


def summarize_release(release: dict[str, Any] | None) -> dict[str, Any]:
    assets = [
        {
            "name": asset.get("name"),
            "size": asset.get("size"),
            "downloads": asset.get("download_count"),
            "url": asset.get("browser_download_url"),
            **classify_release_asset(str(asset.get("name") or "")),
        }
        for asset in (release or {}).get("assets") or []
    ]
    binaries = [asset for asset in assets if asset["kind"] == "binary"]
    windows = [asset for asset in binaries if asset["platform"] == "windows"]
    return {
        "latest": {
            "tag": release.get("tag_name"),
            "publishedAt": release.get("published_at"),
            "url": release.get("html_url"),
            "channel": "prerelease" if release.get("prerelease") else "stable",
            "assets": assets,
        } if release is not None else None,
        "totalAssetDownloads": sum(int(asset["downloads"] or 0) for asset in assets),
        "arm64Assets": [asset["name"] for asset in binaries if asset["architecture"] == "arm64"],
        "x64Assets": [asset["name"] for asset in binaries if asset["architecture"] == "x64"],
        "windowsAssets": [asset["name"] for asset in windows],
        "windowsArm64Assets": [asset["name"] for asset in windows if asset["architecture"] == "arm64"],
        "windowsX64Assets": [asset["name"] for asset in windows if asset["architecture"] == "x64"],
        "sourceAssets": [asset["name"] for asset in assets if asset["kind"] == "source"],
        "checksumAssets": [asset["name"] for asset in assets if asset["kind"] == "checksum"],
        "unknownAssets": [asset["name"] for asset in assets if asset["kind"] == "unknown"],
    }


def release_summary(
    releases: list[dict[str, Any]],
    *,
    repository: str | None = None,
    collection_status: str = "provided",
    release_limit: int | None = None,
) -> dict[str, Any]:
    published = [release for release in releases if not release.get("draft")]
    stable = [release for release in published if not release.get("prerelease")]
    prereleases = [release for release in published if release.get("prerelease")]
    selected = (stable or prereleases or [None])[0]
    result = summarize_release(selected)
    evidence = []
    for release in published:
        summary = summarize_release(release)
        if summary["windowsArm64Assets"]:
            evidence.append(
                {
                    "repository": repository,
                    "tag": summary["latest"]["tag"],
                    "channel": summary["latest"]["channel"],
                    "url": summary["latest"]["url"],
                    "assets": summary["windowsArm64Assets"],
                }
            )
    result.update(
        {
            "channels": {
                "stable": summarize_release(stable[0]) if stable else None,
                "prerelease": summarize_release(prereleases[0]) if prereleases else None,
            },
            "observedWindowsArm64Releases": evidence,
            "coverage": {
                "repository": repository,
                "scope": "provided-releases" if collection_status == "provided" else "github-release-page",
                "collectionStatus": collection_status,
                "releaseLimit": release_limit,
                "returnedReleaseCount": len(releases),
                "stableReleaseCount": len(stable),
                "prereleaseCount": len(prereleases),
                "draftsExcluded": len(releases) - len(published),
                "historyComplete": False,
                "selection": "first-stable-else-first-prerelease-in-response-order",
            },
        }
    )
    return result


def parse_release_repository(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", value):
        raise argparse.ArgumentTypeError("--release-repo must be a GitHub owner/repo slug")
    if value.split("/")[1] in {".", ".."}:
        raise argparse.ArgumentTypeError("--release-repo requires a repository name")
    return value


def collect_releases(
    repository: str,
    token: str | None,
    warnings: list[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    releases = safe_github_get(
        f"/repos/{repository}/releases?per_page=10", token, warnings, None
    )
    return releases or [], {
        "collection_status": "failed" if releases is None else "collected",
        "release_limit": 10,
    }


def score_impact(stars: int, downloads: int) -> int:
    star_component = min(7.0, math.log10(max(stars, 1) + 1) * 1.8)
    download_component = min(3.0, math.log10(max(downloads, 1) + 1) * 0.6)
    return max(1, min(10, round(star_component + download_component)))


def estimate_effort(porting_readiness: int) -> dict[str, Any]:
    if porting_readiness >= 8:
        return {
            "band": "small",
            "minimumDays": 2,
            "maximumDays": 7,
            "rationale": "Mostly managed code with limited architecture-specific dependency or packaging work.",
        }
    if porting_readiness >= 6:
        return {
            "band": "moderate",
            "minimumDays": 5,
            "maximumDays": 14,
            "rationale": "A focused port is plausible after proving native dependencies and packaging.",
        }
    if porting_readiness >= 4:
        return {
            "band": "substantial",
            "minimumDays": 7,
            "maximumDays": 28,
            "rationale": "The port needs explicit subsystem, dependency, installer, or plugin fallbacks.",
        }
    return {
        "band": "large",
        "minimumDays": 21,
        "maximumDays": 60,
        "rationale": "Native code, committed binaries, packaging, or weak test coverage require staged investigation.",
    }


def assess_delivery_window(
    effort: dict[str, Any],
    delivery_window_days: int | None,
) -> dict[str, Any]:
    if delivery_window_days is None:
        return {
            "days": None,
            "fit": "not-assessed",
            "rationale": "No delivery window was supplied; readiness and effort are reported independently.",
        }
    if delivery_window_days >= int(effort["maximumDays"]):
        fit = "strong"
        rationale = (
            f"The estimated {effort['minimumDays']}-{effort['maximumDays']} day effort "
            f"fits within the {delivery_window_days} day delivery window."
        )
    elif delivery_window_days >= int(effort["minimumDays"]):
        fit = "conditional"
        rationale = (
            f"The {delivery_window_days} day delivery window overlaps the estimated "
            f"{effort['minimumDays']}-{effort['maximumDays']} day effort and requires reduced scope or fallbacks."
        )
    else:
        fit = "unlikely"
        rationale = (
            f"The {delivery_window_days} day delivery window is shorter than the estimated "
            f"{effort['minimumDays']}-{effort['maximumDays']} day effort."
        )
    return {
        "days": delivery_window_days,
        "fit": fit,
        "rationale": rationale,
    }


def build_assessment(
    metadata: dict[str, Any],
    releases: list[dict[str, Any]],
    paths: list[str],
    contents: dict[str, str],
    issues: list[dict[str, Any]],
    pull_requests: list[dict[str, Any]],
    source_mode: str,
    warnings: list[str],
    delivery_window_days: int | None = None,
    *,
    related_releases: dict[str, list[dict[str, Any]]] | None = None,
    release_coverage: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    combined_text = "\n".join(contents.values()).lower()
    combined_paths = "\n".join(paths).lower()
    searchable = f"{combined_paths}\n{combined_text}"
    repository_name = metadata.get("full_name")
    coverage = release_coverage or {}
    release = release_summary(
        releases, repository=repository_name, **coverage.get(repository_name, {})
    )
    release["relatedRepositories"] = [
        release_summary(items, repository=name, **coverage.get(name, {}))
        for name, items in (related_releases or {}).items()
    ]
    release["unsearchedChannels"] = [
        "Release history beyond the supplied responses",
        "Related repositories not explicitly supplied",
        "CI artifacts, external download sites, and package registries",
    ]
    arm_release_evidence = [
        evidence
        for summary in [release, *release["relatedRepositories"]]
        for evidence in summary["observedWindowsArm64Releases"]
    ]
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
    known_non_arm_windows = any(
        asset["kind"] == "binary"
        and asset["platform"] == "windows"
        and asset["architecture"] in {"x64", "x86"}
        for asset in (release["latest"] or {}).get("assets", [])
    )
    arm_gap = known_non_arm_windows and not release["windowsArm64Assets"]
    gap_status = (
        "windows-arm64-observed" if release["windowsArm64Assets"]
        else "windows-arm64-not-observed" if arm_gap
        else "unknown"
    )

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
                "evidence": (
                    f"Selected {release['latest']['channel']} release {release['latest']['tag']} "
                    f"in {repository_name} has Windows x86/x64 binary labels but no Windows ARM64 binary label. "
                    "This is a channel-local observation, not proof that the project is unported."
                ),
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
    readiness_score = max(1, min(10, 10 - risk_score + managed_bonus + arm_evidence_bonus))
    downloads = int(release["totalAssetDownloads"])
    stars = int(metadata.get("stargazers_count") or 0)
    impact_score = score_impact(stars, downloads)
    effort = estimate_effort(readiness_score)
    delivery_window = assess_delivery_window(effort, delivery_window_days)
    if readiness_score >= 7:
        recommendation = "Strong native ARM64 candidate; proceed to dependency proof and architecture-isolated packaging."
    elif readiness_score >= 5:
        recommendation = "Viable native ARM64 candidate with explicit subsystem, dependency, installer, or plugin fallbacks."
    else:
        recommendation = "High-risk candidate; complete architecture proof and dependency replacement planning before a full port."
    if arm_release_evidence:
        recommendation = (
            "Windows ARM64-labeled binaries were observed in the inspected releases. "
            "Verify their native architecture and remaining distribution gaps before proposing a new port."
        )
    else:
        recommendation = (
            "Windows ARM64 release availability is unconfirmed; inspect other distribution channels "
            "before treating this project as unported. " + recommendation
        )
    if delivery_window_days is not None:
        recommendation = f"{recommendation} {delivery_window['rationale']}"

    return {
        "schemaVersion": 2,
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
            "latestReleaseHasArm64Asset": bool(release["windowsArm64Assets"]),
            "latestReleaseHasX64Asset": bool(release["windowsX64Assets"]),
            "latestReleaseHasWindowsAsset": bool(release["windowsAssets"]),
            "releaseAssetGap": arm_gap,
            "releaseAssetGapStatus": gap_status,
            "windowsArm64Availability": "observed" if arm_release_evidence else "unconfirmed",
            "windowsArm64Evidence": arm_release_evidence,
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
            "portingReadiness": readiness_score,
        },
        "effortEstimate": effort,
        "deliveryWindow": delivery_window,
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
        f"- Selected release: `{(report['release']['latest'] or {}).get('tag')}` "
        f"({(report['release']['latest'] or {}).get('channel', 'none observed')})",
        "",
        "## Decision",
        "",
        report["recommendation"],
        "",
        "| Score | Value |",
        "|---|---:|",
        f"| Customer impact | {scores['customerImpact']}/10 |",
        f"| Technical risk | {scores['technicalRisk']}/10 |",
        f"| Porting readiness | {scores['portingReadiness']}/10 |",
        "",
        "## Effort and Delivery Window",
        "",
        f"- Estimated effort: {report['effortEstimate']['minimumDays']}-{report['effortEstimate']['maximumDays']} days ({report['effortEstimate']['band']})",
        "- Effort bands are heuristic planning ranges, not delivery commitments.",
        f"- Delivery window: {report['deliveryWindow']['days'] if report['deliveryWindow']['days'] is not None else 'not supplied'}",
        f"- Window fit: {report['deliveryWindow']['fit']}",
        f"- Rationale: {report['deliveryWindow']['rationale']}",
        "",
        "## Architecture Gap",
        "",
        f"- Selected release has Windows x64 binary label: {report['architecture']['latestReleaseHasX64Asset']}",
        f"- Selected release has Windows binary label: {report['architecture']['latestReleaseHasWindowsAsset']}",
        f"- Selected release has Windows ARM64 binary label: {report['architecture']['latestReleaseHasArm64Asset']}",
        f"- Selected-release gap status: {report['architecture']['releaseAssetGapStatus']}",
        f"- Windows ARM64 availability across inspected releases: {report['architecture']['windowsArm64Availability']}",
        f"- Source x64 references: {report['architecture']['x64ReferenceCount']}",
        f"- Source Arm references: {report['architecture']['arm64ReferenceCount']}",
        f"- Committed native binary candidates: {report['architecture']['committedBinaryCount']}",
        "",
        "## Release Coverage",
        "",
        "Asset classifications use filenames only; no binary architecture or functionality was verified.",
        "A missing Windows ARM64 label in one channel does not establish that a project is unported.",
        "",
    ]
    for summary in [report["release"], *report["release"]["relatedRepositories"]]:
        coverage = summary["coverage"]
        lines.append(
            f"- `{coverage['repository']}`: {coverage['collectionStatus']}; "
            f"{coverage['returnedReleaseCount']} release records inspected "
            f"({coverage['stableReleaseCount']} stable, {coverage['prereleaseCount']} prerelease, "
            f"{coverage['draftsExcluded']} drafts excluded); "
            f"response limit: {coverage['releaseLimit'] or 'caller supplied'}; full history not established."
        )
        for channel, channel_summary in summary["channels"].items():
            if channel_summary is None:
                lines.append(f"  - {channel}: none observed in the supplied response.")
                continue
            latest = channel_summary["latest"]
            lines.append(
                f"  - {channel}: `{latest['tag']}`, published {latest['publishedAt']}, "
                f"{latest['url']}; Windows ARM64 labels: "
                f"{', '.join(channel_summary['windowsArm64Assets']) or 'none observed'}; "
                f"unclassified assets: {len(channel_summary['unknownAssets'])}."
            )
    for evidence in report["architecture"]["windowsArm64Evidence"]:
        lines.append(
            f"- Windows ARM64 evidence: `{evidence['repository']}` `{evidence['tag']}` "
            f"({evidence['channel']}): {', '.join(evidence['assets'])}; {evidence['url']}."
        )
    lines.extend(
        [
            "- Not searched: " + "; ".join(report["release"]["unsearchedChannels"]) + ".",
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
    )
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
    parser.add_argument(
        "--release-repo",
        action="append",
        default=[],
        type=parse_release_repository,
        help="Explicit related GitHub owner/repo to inspect for releases (repeatable; no link crawling).",
    )
    parser.add_argument("--local-path", help="Optional local clone for complete source scanning")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow a dirty local tree and record its status. Clean trees are required by default.",
    )
    parser.add_argument(
        "--delivery-window-days",
        type=int,
        help="Optional project delivery window. Readiness and effort remain independent when omitted.",
    )
    parser.add_argument("--output", required=True, help="Output directory")
    parser.add_argument("--github-token-env", default="GITHUB_TOKEN")
    args = parser.parse_args()
    if args.delivery_window_days is not None and args.delivery_window_days <= 0:
        raise ValueError("--delivery-window-days must be greater than zero")

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
    if not args.local_path and not re.fullmatch(r"[0-9a-f]{40}", str(metadata["_analyzed_commit"] or "")):
        raise ValueError("Remote source assessment requires a resolved immutable commit.")
    primary_repository = f"{owner}/{repository}"
    releases, primary_coverage = collect_releases(primary_repository, token, warnings)
    release_coverage = {metadata.get("full_name", primary_repository): primary_coverage}
    related_releases: dict[str, list[dict[str, Any]]] = {}
    seen_repositories = {primary_repository.lower()}
    for release_repository in args.release_repo:
        if release_repository.lower() in seen_repositories:
            continue
        seen_repositories.add(release_repository.lower())
        related_releases[release_repository], release_coverage[release_repository] = collect_releases(
            release_repository, token, warnings
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
        source_commit = metadata["_analyzed_commit"]
        tree_response = github_get(
            f"/repos/{owner}/{repository}/git/trees/{source_commit}?recursive=1",
            token,
        )
        tree = list(tree_response.get("tree") or [])
        if tree_response.get("truncated"):
            warnings.append("GitHub truncated the source tree response; use a pinned local clone for complete source coverage.")
        paths = sorted(str(entry.get("path")) for entry in tree if entry.get("type") == "blob")
        selected = select_remote_text_paths(tree)
        contents = fetch_remote_contents(owner, repository, source_commit, selected, warnings)
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
        args.delivery_window_days,
        related_releases=related_releases,
        release_coverage=release_coverage,
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
                "effortEstimate": report["effortEstimate"],
                "deliveryWindow": report["deliveryWindow"],
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
