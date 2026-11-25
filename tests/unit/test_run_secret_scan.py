"""Unit tests for scripts/run_secret_scan.py target expansion and baseline isolation."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest
from scripts import run_secret_scan

ROOT = Path(__file__).resolve().parents[2]


def _init_git_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True)


def _write_minimal_baseline(repo_dir: Path) -> Path:
    baseline_data = json.loads((ROOT / ".secrets.baseline").read_text(encoding="utf-8"))
    baseline_data["results"] = {}
    baseline_path = repo_dir / ".secrets.baseline"
    baseline_path.write_text(json.dumps(baseline_data, indent=2) + "\n", encoding="utf-8")
    return baseline_path


def test_expand_targets_resolves_workspace_and_packages_into_concrete_files() -> None:
    expected_root = [
        entry
        for entry in subprocess.check_output(
            [
                "git",
                "--literal-pathspecs",
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            cwd=ROOT,
        )
        .decode("utf-8")
        .split("\0")
        if entry and (ROOT / entry).is_file()
    ]
    resolved_root = run_secret_scan.expand_targets(["."], repo_root=ROOT, cwd=ROOT)
    assert len(resolved_root) >= 1039
    assert resolved_root == sorted(set(expected_root))
    assert "." not in resolved_root
    assert "packages" not in resolved_root

    resolved_packages = run_secret_scan.expand_targets(["packages"], repo_root=ROOT, cwd=ROOT)
    assert len(resolved_packages) > 200
    assert all(path.startswith("packages/") for path in resolved_packages)
    assert all((ROOT / path).is_file() for path in resolved_packages)


def test_expand_targets_includes_untracked_and_excludes_ignored_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _write_minimal_baseline(repo)
    (repo / ".gitignore").write_text("ignored_dir/\n*.secret.env\n", encoding="utf-8")

    pkg_dir = repo / "packages" / "core"
    pkg_dir.mkdir(parents=True)
    tracked_file = pkg_dir / "tracked.py"
    tracked_file.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", ".gitignore", ".secrets.baseline", "packages/core/tracked.py"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    untracked_file = pkg_dir / "untracked.py"
    untracked_file.write_text("VALUE = 2\n", encoding="utf-8")

    ignored_dir = repo / "ignored_dir"
    ignored_dir.mkdir()
    ignored_file = ignored_dir / "skip.py"
    ignored_file.write_text("VALUE = 3\n", encoding="utf-8")
    ignored_env = repo / "local.secret.env"
    ignored_env.write_text("VALUE = 4\n", encoding="utf-8")

    expanded_all = run_secret_scan.expand_targets(["."], repo_root=repo, cwd=repo)
    assert set(expanded_all) == {
        ".gitignore",
        ".secrets.baseline",
        "packages/core/tracked.py",
        "packages/core/untracked.py",
    }

    expanded_pkg = run_secret_scan.expand_targets(["packages/core"], repo_root=repo, cwd=repo)
    assert expanded_pkg == ["packages/core/tracked.py", "packages/core/untracked.py"]

    expanded_untracked = run_secret_scan.expand_targets(
        ["packages/core/untracked.py"], repo_root=repo, cwd=repo
    )
    assert expanded_untracked == ["packages/core/untracked.py"]

    # Ignored targets resolve to zero scannable files and fail closed.
    with pytest.raises(run_secret_scan.SecretScanTargetError, match="zero scannable files"):
        run_secret_scan.expand_targets(["ignored_dir"], repo_root=repo, cwd=repo)

    with pytest.raises(run_secret_scan.SecretScanTargetError, match="zero scannable files"):
        run_secret_scan.expand_targets(["local.secret.env"], repo_root=repo, cwd=repo)


def test_main_fails_closed_on_missing_empty_or_outside_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _write_minimal_baseline(repo)
    valid_dir = repo / "packages"
    valid_dir.mkdir()
    (valid_dir / "mod.py").write_text("X = 1\n", encoding="utf-8")
    empty_dir = repo / "empty_pkg"
    empty_dir.mkdir()
    outside_file = tmp_path / "outside.py"
    outside_file.write_text("X = 2\n", encoding="utf-8")

    subprocess.run(["git", "add", ".secrets.baseline", "packages/mod.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    monkeypatch.chdir(repo)

    called = False

    def _never_called(argv: list[str]) -> int:
        nonlocal called
        called = True
        return 0

    assert run_secret_scan.main(["missing_dir"], hook_runner=_never_called) == 1
    assert "does not exist" in capsys.readouterr().err

    assert run_secret_scan.main(["empty_pkg"], hook_runner=_never_called) == 1
    assert "zero scannable files" in capsys.readouterr().err

    # Even when combined with a valid target, any empty/missing target fails closed.
    assert run_secret_scan.main(["packages", "empty_pkg"], hook_runner=_never_called) == 1
    assert "zero scannable files" in capsys.readouterr().err

    assert run_secret_scan.main([str(outside_file)], hook_runner=_never_called) == 1
    assert "outside repository root" in capsys.readouterr().err

    assert called is False


def test_temp_baseline_copy_prevents_disk_mutation_and_maps_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    baseline_path = _write_minimal_baseline(repo)
    (repo / "app.py").write_text("OK = True\n", encoding="utf-8")
    subprocess.run(["git", "add", ".secrets.baseline", "app.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    original_bytes = baseline_path.read_bytes()
    captured_calls: list[list[str]] = []

    def _mutating_line_shift_runner(argv: list[str]) -> int:
        captured_calls.append(list(argv))
        assert argv[0] == "--baseline"
        temp_baseline = Path(argv[1])
        assert temp_baseline != baseline_path
        assert temp_baseline.name == ".secrets.baseline"
        assert temp_baseline.read_bytes() == original_bytes
        # Simulate detect-secrets rewriting the baseline on line shifts (return code 3).
        temp_baseline.write_text('{"mutated": true}\n', encoding="utf-8")
        return 3

    monkeypatch.chdir(repo)
    assert run_secret_scan.main([], hook_runner=_mutating_line_shift_runner) == 0
    assert baseline_path.read_bytes() == original_bytes
    assert len(captured_calls) == 1
    assert set(captured_calls[0][2:]) == {".secrets.baseline", "app.py"}

    assert run_secret_scan.main(["app.py"], hook_runner=lambda _argv: 1) == 1
    assert baseline_path.read_bytes() == original_bytes


def test_invoke_detect_secrets_hook_normalizes_baseline_basename_and_handles_system_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: dict[str, object] = {"cache_bust": 0, "configured_filenames": [], "argv": None}

    fake_pkg = types.ModuleType("detect_secrets")
    fake_core = types.ModuleType("detect_secrets.core")
    fake_baseline = types.ModuleType("detect_secrets.core.baseline")
    fake_settings = types.ModuleType("detect_secrets.settings")
    fake_hook = types.ModuleType("detect_secrets.pre_commit_hook")

    def _orig_configure(baseline_dict: dict[str, object], filename: str = "") -> dict[str, object]:
        cast_list = recorded["configured_filenames"]
        assert isinstance(cast_list, list)
        cast_list.append(filename)
        return baseline_dict

    def _cache_bust() -> None:
        recorded["cache_bust"] = int(recorded["cache_bust"]) + 1  # type: ignore[arg-type]

    def _hook_main(argv: list[str]) -> int:
        recorded["argv"] = argv
        fake_baseline.configure_settings_from_baseline({}, filename=argv[1])
        return 0

    fake_settings.configure_settings_from_baseline = _orig_configure  # type: ignore[attr-defined]
    fake_settings.cache_bust = _cache_bust  # type: ignore[attr-defined]
    fake_baseline.configure_settings_from_baseline = _orig_configure  # type: ignore[attr-defined]
    fake_hook.main = _hook_main  # type: ignore[attr-defined]
    fake_core.baseline = fake_baseline  # type: ignore[attr-defined]
    fake_pkg.core = fake_core  # type: ignore[attr-defined]
    fake_pkg.settings = fake_settings  # type: ignore[attr-defined]
    fake_pkg.pre_commit_hook = fake_hook  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "detect_secrets", fake_pkg)
    monkeypatch.setitem(sys.modules, "detect_secrets.core", fake_core)
    monkeypatch.setitem(sys.modules, "detect_secrets.core.baseline", fake_baseline)
    monkeypatch.setitem(sys.modules, "detect_secrets.settings", fake_settings)
    monkeypatch.setitem(sys.modules, "detect_secrets.pre_commit_hook", fake_hook)

    rc = run_secret_scan._invoke_detect_secrets_hook(
        ["--baseline", "/tmp/imednet-secrets-xyz/.secrets.baseline", "app.py"]
    )
    assert rc == 0
    assert recorded["configured_filenames"] == [".secrets.baseline"]
    assert recorded["cache_bust"] == 2

    def _raising_exit(_argv: list[str]) -> int:
        raise SystemExit(2)

    fake_hook.main = _raising_exit  # type: ignore[attr-defined]
    assert (
        run_secret_scan._invoke_detect_secrets_hook(
            ["--baseline", "/tmp/imednet-secrets-xyz/.secrets.baseline", "app.py"]
        )
        == 1
    )


@pytest.mark.skipif(
    importlib.util.find_spec("detect_secrets") is None,
    reason="detect-secrets is installed only in detached lint/uvx environments",
)
def test_real_detect_secrets_catches_tracked_and_untracked_leaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    baseline_path = _write_minimal_baseline(repo)
    pkg = repo / "packages"
    pkg.mkdir()
    clean_file = pkg / "clean.py"
    clean_file.write_text("SAFE_CONSTANT = 'hello'\n", encoding="utf-8")
    subprocess.run(["git", "add", ".secrets.baseline", "packages/clean.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    baseline_before = baseline_path.read_bytes()
    monkeypatch.chdir(repo)
    assert run_secret_scan.main(["."]) == 0
    assert run_secret_scan.main(["packages"]) == 0

    fake_token = "ghp_" + ("1" * 36)
    untracked_leak = pkg / "untracked_leak.py"
    untracked_leak.write_text(f'TOKEN = "{fake_token}"\n', encoding="utf-8")
    assert run_secret_scan.main(["."]) == 1
    assert run_secret_scan.main(["packages"]) == 1
    assert run_secret_scan.main(["packages/untracked_leak.py"]) == 1
    untracked_leak.unlink()

    clean_file.write_text(f'API_SECRET = "{fake_token}"\n', encoding="utf-8")
    assert run_secret_scan.main(["."]) == 1
    assert run_secret_scan.main(["packages"]) == 1
    assert baseline_path.read_bytes() == baseline_before
