#!/usr/bin/env python3
"""Validate a GitHub execution trigger against its project issue detail."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


PROJECT_ID = re.compile(r"(?im)^Project issue:\s*(\d{6}-\d{2})\s*$")
ISSUE_STATUS = re.compile(r"(?im)^Status:\s*(new|resolved)\s*$")
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
    detail = repo / "docs/issues/issues-detail" / f"{issue_id}.md"
    if not detail.is_file():
        raise ValueError(f"Missing detail file for {issue_id}")
    content = detail.read_text(encoding="utf-8")
    status = ISSUE_STATUS.search(content)
    if not status or status.group(1).lower() != "new":
        raise ValueError(f"{issue_id} detail file does not have status=new")
    missing = [heading for heading in REQUIRED_SECTIONS if f"## {heading}" not in content]
    if missing:
        raise ValueError(f"Detail file is missing sections: {', '.join(missing)}")
    return issue_id, detail


def mark_resolved(repo: Path, issue_id: str) -> None:
    """Mark one verified project issue detail resolved."""
    detail = repo / "docs/issues/issues-detail" / f"{issue_id}.md"
    if not detail.is_file():
        raise ValueError(f"Missing detail file for {issue_id}")
    content = detail.read_text(encoding="utf-8")
    resolved_content, replacements = re.subn(
        r"(?im)^(Status:\s*)new(\s*)$", r"\1resolved\2", content
    )
    if replacements != 1:
        raise ValueError(f"{issue_id} detail file does not have status=new")
    detail.write_text(resolved_content, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--issue-body-file", type=Path)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--mark-resolved", action="store_true")
    parser.add_argument("--project-issue")
    args = parser.parse_args()
    repo = args.repo.resolve()
    if args.mark_resolved:
        if not args.project_issue:
            parser.error("--project-issue is required with --mark-resolved")
        mark_resolved(repo, args.project_issue)
        return 0
    if args.issue_body_file is None:
        parser.error("--issue-body-file is required unless --mark-resolved is used")
    issue_id, detail = validate(repo, args.issue_body_file.read_text(encoding="utf-8"))
    output = f"project_issue={issue_id}\nbranch=codex/{issue_id}\ndetail_file={detail.relative_to(args.repo).as_posix()}\n"
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write(output)
    else:
        print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
