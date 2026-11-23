from pathlib import Path

from job_search_agent.cli import resolve_home

PROJECT = Path(__file__).resolve().parents[1]


def test_explicit_home_wins(tmp_path):
    assert resolve_home(tmp_path) == tmp_path.resolve()


def test_cwd_with_config_is_used(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    monkeypatch.chdir(tmp_path)
    assert resolve_home(None) == tmp_path


def test_unrelated_folder_falls_back_to_the_project_not_an_empty_copy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # e.g. ~/Downloads: no config/ here
    assert resolve_home(None) == PROJECT
