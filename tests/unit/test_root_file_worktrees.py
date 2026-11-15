"""Check root hygiene against real linked-worktree metadata and stray files."""

import importlib.util
from pathlib import Path

import pytest


def load_guard(tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts" / "check_root_files.py"
    spec = importlib.util.spec_from_file_location("root_file_guard", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.__file__ = str(tmp_path / "scripts" / "check_root_files.py")
    return module


def test_linked_worktree_git_file_is_allowed(tmp_path):
    (tmp_path / ".git").write_text("gitdir: /synthetic/repository/.git/worktrees/check\n")
    load_guard(tmp_path).main()


def test_worktree_still_rejects_stray_root_output(tmp_path, capsys):
    (tmp_path / ".git").write_text("gitdir: /synthetic/repository/.git/worktrees/check\n")
    (tmp_path / "temporary-output.log").write_text("synthetic output\n")
    with pytest.raises(SystemExit) as caught:
        load_guard(tmp_path).main()
    assert caught.value.code == 1
    assert "temporary-output.log" in capsys.readouterr().out
