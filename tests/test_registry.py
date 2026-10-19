from datetime import datetime
from pathlib import Path

from multi_agent_ai.cli import main
from multi_agent_ai.registry import ReservationError, read_registry, require_reservations, reservation_conflicts
from multi_agent_ai.state import init_state_root


def write_entry(root: Path, agent: str, *, updated="2026-10-20 10:00", files="src/", status="working"):
    path = root / "registry" / f"{agent}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"agent: {agent}\nstarted: 2026-10-20 09:00\nupdated: {updated}\n"
        f"task: job-1\nfiles: {files}\nstatus: {status}\ncontact: test\n",
        encoding="utf-8",
    )


def test_registry_reads_entries_and_preserves_invalid_files(tmp_path):
    root = init_state_root(tmp_path)
    write_entry(root.path, "w1", files="src/, tests/")
    (root.path / "registry" / "bad.md").write_text("agent: bad\n", encoding="utf-8")
    entries = read_registry(root.path)
    assert entries["w1"].ok and entries["w1"].value.file_items == ("src", "tests")
    assert not entries["bad"].ok


def test_active_reservation_rejects_parent_and_glob_overlap(tmp_path):
    root = init_state_root(tmp_path)
    write_entry(root.path, "w1", files="src/")
    assert reservation_conflicts(root.path, "src/pkg/a.py", now=datetime(2026, 10, 20, 11))
    try:
        require_reservations(root.path, "src/pkg/a.py", now=datetime(2026, 10, 20, 11))
    except ReservationError as exc:
        assert "w1" in str(exc)
    else:
        raise AssertionError("overlapping reservation was accepted")


def test_disjoint_areas_do_not_conflict_and_globs_are_conservative(tmp_path):
    root = init_state_root(tmp_path)
    write_entry(root.path, "src-a", files="src/a/")
    write_entry(root.path, "file-a", files="a.py")
    write_entry(root.path, "glob", files="docs/*.md")
    assert not reservation_conflicts(root.path, "src/b/file.py", now=datetime(2026, 10, 20, 11))
    assert not reservation_conflicts(root.path, "b.py", now=datetime(2026, 10, 20, 11))
    assert not reservation_conflicts(root.path, "srcx/file.py", now=datetime(2026, 10, 20, 11))
    assert reservation_conflicts(root.path, "docs/readme.md", now=datetime(2026, 10, 20, 11))


def test_parenthesised_paths_are_preserved_except_trailing_annotations(tmp_path):
    root = init_state_root(tmp_path)
    write_entry(root.path, "paren", files="src/(app)/page.tsx")
    assert reservation_conflicts(root.path, "src/(app)/page.tsx", now=datetime(2026, 10, 20, 11))
    write_entry(root.path, "annotated", files="src/app.tsx (generated)")
    assert reservation_conflicts(root.path, "src/app.tsx", now=datetime(2026, 10, 20, 11))


def test_stale_and_inactive_entries_release_reservations(tmp_path):
    root = init_state_root(tmp_path)
    write_entry(root.path, "stale", updated="2026-10-20 06:00", files="src/")
    write_entry(root.path, "done", files="docs/", status="done")
    now = datetime(2026, 10, 20, 10, 1)
    assert not reservation_conflicts(root.path, "src/a.py", now=now)
    assert not reservation_conflicts(root.path, "docs/a.md", now=now)


def test_date_only_updated_releases_reservation_with_warning(tmp_path):
    root = init_state_root(tmp_path)
    write_entry(root.path, "date-only", updated="2026-10-20", files="src/")
    parsed = read_registry(root.path)["date-only"]
    assert parsed.ok and any("reservation is released" in warning for warning in parsed.warnings)
    assert not reservation_conflicts(root.path, "src/a.py", now=datetime(2026, 10, 20, 11))


def test_registry_cli_lists_and_reports_conflict(tmp_path, capsys):
    root = init_state_root(tmp_path)
    write_entry(root.path, "w1", files="src/")
    assert main(["registry", "--state-root", str(root.path), "--files", "src/app.py", "--now", "2026-10-20 11:00"]) == 1
    assert "conflict\tw1" in capsys.readouterr().out
    assert main(["registry", "--state-root", str(root.path), "--json"]) == 0
    assert '"agent": "w1"' in capsys.readouterr().out
