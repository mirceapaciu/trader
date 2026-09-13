#!/usr/bin/env python3
"""Validate a GitHub execution trigger against the project issue registry."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


PROJECT_ID = re.compile(r"(?im)^Project issue:\s*(\d{6}-\d{2})\s*$")
REQUIRED_SECTIONS = (
    "Problem Statement",
    "Verified Evidence",
    "Expected Behavior",
    "Acceptance Criteria",
    "Test Plan",
)


def validate(repo: Path, issue_body: str) -> tuple[str, Path]:
    ids = PROJECT_ID.findall(issue_body)
    if len(ids) != 1:
        raise ValueError("GitHub issue must contain exactly one 'Project issue: YYMMDD-XX' line")
    issue_id = ids[0]
    index = (repo / "docs/issues/issues-index.md").read_text(encoding="utf-8")
    row = re.search(
        rf"(?m)^\|\s*{re.escape(issue_id)}\s*\|.*\|\s*new\s*\|\s*([^|]+?)\s*\|$",
        index,
    )
    if not row:
        raise ValueError(f"{issue_id} is absent from the index or does not have status=new")
    detail = repo / row.group(1).strip()
    if not detail.is_file() or not detail.resolve().is_relative_to(repo.resolve()):
        raise ValueError(f"Invalid or missing detail file for {issue_id}")
    content = detail.read_text(encoding="utf-8")
    missing = [heading for heading in REQUIRED_SECTIONS if f"## {heading}" not in content]
    if missing:
        raise ValueError(f"Detail file is missing sections: {', '.join(missing)}")
    return issue_id, detail


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--issue-body-file", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    issue_id, detail = validate(args.repo.resolve(), args.issue_body_file.read_text(encoding="utf-8"))
    output = f"project_issue={issue_id}\nbranch=codex/{issue_id}\ndetail_file={detail.relative_to(args.repo).as_posix()}\n"
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write(output)
    else:
        print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

