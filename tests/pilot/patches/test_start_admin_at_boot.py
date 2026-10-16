from __future__ import annotations

from pathlib import Path

from pilot.managers.processes.systemd import SystemdProcessManager
from pilot.patches.start_admin_at_boot import run

_TOML = """[bench]
name = "{name}"
python = "3.14"

[production]
enabled = {production}
process_manager = "systemd"

[admin]
domain = "admin-{name}.example.com"
"""


def _bench(root: Path, name: str, production: bool) -> None:
    (root / name).mkdir(parents=True)
    (root / name / "bench.toml").write_text(_TOML.format(name=name, production=str(production).lower()))


def test_production_benches_start_the_admin_at_boot_once(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(SystemdProcessManager, "is_configured", lambda self: True)
    monkeypatch.setattr(SystemdProcessManager, "write_config", lambda self: calls.append(f"units:{self.bench.config.name}"))
    monkeypatch.setattr(SystemdProcessManager, "reload_manager_config", lambda self: None)
    monkeypatch.setattr(SystemdProcessManager, "enable_admin_at_boot", lambda self: calls.append(f"enable:{self.bench.config.name}"))
    root = tmp_path / "benches"
    _bench(root, "one", production=True)
    _bench(root, "two", production=False)

    run(root)
    run(root)

    assert calls == ["units:one", "enable:one"]
