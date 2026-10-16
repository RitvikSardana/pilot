from __future__ import annotations

from pathlib import Path

import pytest

from pilot.managers.database import MariaDBManager
from pilot.patches.keep_mariadb_out_of_swap import run

_TOML = """[bench]
name = "{name}"
python = "3.14"
db_type = "{db_type}"
"""


def _bench(root: Path, name: str, db_type: str) -> None:
    (root / name).mkdir(parents=True)
    (root / name / "bench.toml").write_text(_TOML.format(name=name, db_type=db_type))


def test_mariadb_benches_keep_mariadb_out_of_swap_once(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(MariaDBManager, "keep_out_of_swap", lambda self: calls.append("keep_out_of_swap"))
    root = tmp_path / "benches"
    _bench(root, "one", "mariadb")
    _bench(root, "two", "postgres")
    (root / "not-a-bench").mkdir()

    run(root)
    run(root)

    assert calls == ["keep_out_of_swap"]


def test_a_failed_attempt_is_not_marked_applied(tmp_path: Path, monkeypatch) -> None:
    def fail(self):
        raise RuntimeError("systemctl failed")

    monkeypatch.setattr(MariaDBManager, "keep_out_of_swap", fail)
    root = tmp_path / "benches"
    _bench(root, "one", "mariadb")

    with pytest.raises(RuntimeError):
        run(root)

    calls: list[str] = []
    monkeypatch.setattr(MariaDBManager, "keep_out_of_swap", lambda self: calls.append("keep_out_of_swap"))
    run(root)
    assert calls == ["keep_out_of_swap"]
