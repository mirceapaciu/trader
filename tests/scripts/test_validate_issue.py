from pathlib import Path

import pytest

from scripts.development.validate_issue import validate


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

