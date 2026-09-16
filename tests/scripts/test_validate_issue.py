from pathlib import Path

import pytest

from scripts.development.validate_issue import mark_resolved, validate


def _registry(tmp_path: Path, *, status: str = "new", sections: bool = True) -> Path:
    detail = tmp_path / "docs/issues/issues-detail/260910-01.md"
    detail.parent.mkdir(parents=True)
    headings = ["Problem Statement", "Verified Evidence", "Expected Behavior", "Acceptance Criteria", "Test Plan"]
    detail.write_text("\n".join(f"## {heading}\nvalue" for heading in headings if sections or heading != "Test Plan"))
    (tmp_path / "docs/issues/issues-index.md").write_text(
        f"| id | title | status | detail_file |\n| 260910-01 | Test | {status} | docs/issues/issues-detail/260910-01.md |\n"
    )
    return tmp_path


def test_validate_accepts_one_new_documented_issue(tmp_path: Path) -> None:
    issue_id, detail = validate(_registry(tmp_path), "Project issue: 260910-01")
    assert issue_id == "260910-01"
    assert detail.name == "260910-01.md"


@pytest.mark.parametrize("body", ["", "Project issue: 260910-01\nProject issue: 260910-02"])
def test_validate_requires_exactly_one_project_issue(tmp_path: Path, body: str) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        validate(_registry(tmp_path), body)


def test_validate_requires_rejects_non_new_issue(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="status=new"):
        validate(_registry(tmp_path, status="resolved"), "Project issue: 260910-01")


def test_validate_requires_all_sections(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Test Plan"):
        validate(_registry(tmp_path, sections=False), "Project issue: 260910-01")


def test_mark_resolved_updates_only_a_new_matching_issue(tmp_path: Path) -> None:
    repo = _registry(tmp_path)

    mark_resolved(repo, "260910-01")

    index = (repo / "docs/issues/issues-index.md").read_text(encoding="utf-8")
    assert "| 260910-01 | Test | resolved |" in index


def test_mark_resolved_rejects_already_resolved_issue(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="status=new"):
        mark_resolved(_registry(tmp_path, status="resolved"), "260910-01")


def test_issue_workflow_marks_resolved_only_after_verification() -> None:
    workflow = (Path(__file__).parents[2] / ".github/workflows/implement-issue.yml").read_text(encoding="utf-8")
    verify_start = workflow.index("- name: Verify implementation")
    resolve_start = workflow.index("- name: Mark verified project issue resolved")

    assert verify_start < resolve_start
    assert "[[:space:]]*new[[:space:]]*" in workflow[verify_start:resolve_start]
    assert "--mark-resolved" in workflow[resolve_start:]


def test_issue_workflow_checkpoints_and_defers_usage_limit_failures() -> None:
    repo = Path(__file__).parents[2]
    workflow = (repo / ".github/workflows/implement-issue.yml").read_text(encoding="utf-8")
    retry_workflow = (repo / ".github/workflows/retry-codex-issues.yml").read_text(encoding="utf-8")

    assert "status=usage_limit" in workflow
    assert "Checkpoint work deferred by Codex usage limit" in workflow
    assert 'git push --set-upstream origin "$BRANCH"' in workflow
    assert "--add-label codex:retry" in workflow
    assert "schedule:" in retry_workflow
    assert "--label codex:retry" in retry_workflow
    assert "gh workflow run implement-issue.yml" in retry_workflow
