from pathlib import Path

from obsmcp.config import Config, configure, load_config


def test_configure_creates_standalone(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    project = tmp_path / "proj"
    project.mkdir()
    cfg = configure(str(project))
    assert cfg.mode == "standalone"
    assert cfg.project_path == str(project.resolve())
    loaded = load_config()
    assert loaded.project_path == cfg.project_path
    assert Path(cfg.local_db_path).parent.exists()


def test_configure_cloud_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    project = tmp_path / "proj"
    project.mkdir()
    cfg = configure(str(project), backend_url="https://api.example.com", api_token="abc")
    assert cfg.mode == "cloud"
    assert cfg.backend_url == "https://api.example.com"


def test_defaults_are_sane():
    cfg = Config()
    assert cfg.llm_model
    assert cfg.perf_log_interval_seconds > 0
    assert cfg.enabled_modules.task_monitor is True
