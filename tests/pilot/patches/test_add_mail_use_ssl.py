from __future__ import annotations

import json
from pathlib import Path

from pilot.config.mail import MailConfig
from pilot.patches.add_mail_use_ssl import run


def _bench(tmp_path: Path, config: dict) -> Path:
    bench = tmp_path / "benches" / "one"
    (bench / "sites").mkdir(parents=True)
    (bench / "bench.toml").write_text('[bench]\nname = "one"\n')
    (bench / "sites" / "common_site_config.json").write_text(json.dumps(config))
    return bench / "sites" / "common_site_config.json"


def test_an_ssl_mailbox_gets_use_ssl(tmp_path: Path) -> None:
    path = _bench(tmp_path, {"mail_server": "smtp.test", "mail_port": 465, "use_tls": 0})

    run(tmp_path / "benches")

    assert json.loads(path.read_text())["use_ssl"] == 1


def test_a_starttls_mailbox_is_left_alone(tmp_path: Path) -> None:
    config = {"mail_server": "smtp.test", "mail_port": 587, "use_tls": 1}
    path = _bench(tmp_path, config)

    run(tmp_path / "benches")

    assert json.loads(path.read_text()) == config


def test_a_starttls_mailbox_reads_without_ssl(tmp_path: Path) -> None:
    """use_ssl wins over the use_tls fallback, so a STARTTLS box written by Central stays STARTTLS."""
    path = _bench(tmp_path, {"mail_server": "smtp.test", "use_tls": 1, "use_ssl": 0})

    assert MailConfig.read(path.parent).use_ssl is False
