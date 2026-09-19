#!/usr/bin/env python3
"""Run every RDMA lab and write the leaderboard grade record."""

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

from build import ROOT, build


def changed_languages():
    root_commit = subprocess.run(
        ["git", "rev-list", "--max-parents=0", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    if len(root_commit) != 1:
        raise ValueError("cannot identify the repository's initial template commit")

    changed = []
    for language in ("c", "cpp", "rust"):
        result = subprocess.run(
            ["git", "diff", "--quiet", root_commit[0], "HEAD", "--", f"transfer-engine/{language}"],
            cwd=ROOT,
            check=False,
        )
        if result.returncode == 1:
            changed.append(language)
        elif result.returncode != 0:
            raise RuntimeError(f"failed to inspect the {language} implementation")
    if not changed:
        raise ValueError("change at least one implementation directory before scoring")
    return changed


def run_lab(library, lab, reports):
    report = reports / f"lab{lab}.xml"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/engine",
            "--rdma",
            "--library",
            str(library),
            "-m",
            f"lab{lab}",
            "-q",
            f"--junitxml={report}",
        ],
        cwd=ROOT,
        check=False,
    )
    if not report.is_file():
        raise RuntimeError(f"lab{lab} did not produce a test report (exit {result.returncode})")
    root = ElementTree.parse(report).getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    if suite is None:
        raise RuntimeError(f"lab{lab} produced an invalid test report")
    total = int(suite.attrib.get("tests", 0))
    failed = int(suite.attrib.get("failures", 0)) + int(suite.attrib.get("errors", 0))
    skipped = int(suite.attrib.get("skipped", 0))
    passed = total - failed - skipped
    return max(0, passed), total


def score_language(language, output):
    reports = output / "reports" / language
    reports.mkdir(parents=True, exist_ok=True)
    try:
        library = build(language)
        results = [run_lab(library, lab, reports) for lab in range(9)]
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"{language}: unable to complete assessment: {error}", file=sys.stderr)
        results = [(0, 1)] * 9
    earned = sum(100 * passed / total for passed, total in results[1:] if total)
    print(f"{language}: {earned:.1f}/800")
    return earned, results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "grader-output")
    args = parser.parse_args()
    assessments = [
        (language, *score_language(language, args.output)) for language in changed_languages()
    ]
    language, earned, results = max(assessments, key=lambda assessment: assessment[1])

    timestamp = datetime.now().astimezone().strftime("%Y_%m_%d_%H_%M_%S")
    score_file = f"{timestamp}.txt"
    lines = [f"Language: {language}"]
    for lab, (passed, total) in enumerate(results):
        lines.append(f"Lab{lab}: {passed}/{total}")
    (args.output / score_file).write_text("\n".join(lines) + "\n")
    (args.output / "latest.json").write_text(f'{{"rdma":"{score_file}"}}\n')

    print(f"RDMA score: {earned:.1f}/800 using {language} (Lab 0 is not scored)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
