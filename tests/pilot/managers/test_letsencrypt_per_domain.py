from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pilot.config import RoutePolicy, SiteConfig
from pilot.managers.letsencrypt import (
    LetsEncryptManager,
    certificate_domains,
    is_public_domain,
    public_domains,
)
from tests.pilot.integrations.test_central_client import _bench


def _site(bench, name: str, **config) -> None:
    site_dir = bench.sites_path / name
    site_dir.mkdir(parents=True, exist_ok=True)
    (site_dir / "site_config.json").write_text(json.dumps(config))


def _obtained(bench) -> list[str]:
    """The sites obtain_all actually asked certbot for."""
    names: list[str] = []
    with (
        patch.object(LetsEncryptManager, "obtain", lambda self, site: names.append(site.name)),
        patch.object(LetsEncryptManager, "obtain_admin", lambda self: names.append("ADMIN")),
    ):
        LetsEncryptManager(bench).obtain_all()
    return names


def test_certificate_domains_are_only_those_terminating_here() -> None:
    site = SiteConfig(
        name="site-a1.zone.example",
        apps=[],
        domains=[{"domain": "shop.customer.com", "tls": True}],
        ssl=False,
    )

    # Both are candidates for TLS; only the custom domain terminates here.
    assert public_domains(site) == ["site-a1.zone.example", "shop.customer.com"]
    assert certificate_domains(site) == ["shop.customer.com"]


def test_edge_tls_site_does_not_get_an_origin_certificate() -> None:
    site = SiteConfig(
        name="site-a1.zone.example",
        apps=[],
        ssl=True,
        route=RoutePolicy("https", "http", "x_forwarded_for"),
    )

    assert public_domains(site) == ["site-a1.zone.example"]
    assert certificate_domains(site) == []


@pytest.mark.parametrize(
    "domain",
    ["local", "site.local", "localhost", "site.localhost", "SITE.LOCAL."],
)
def test_local_names_are_not_public_certificate_domains(domain: str) -> None:
    assert is_public_domain(domain) is False


def test_a_custom_domain_gets_a_certificate_though_the_admin_is_proxied(tmp_path: Path) -> None:
    """The documented cloud case: admin.tls off, site ssl off, one custom domain
    whose TLS ends on this host."""
    bench = _bench(tmp_path)
    bench.config.admin.tls = False
    _site(bench, "site-a1.zone.example", ssl=False, domains=[{"domain": "shop.customer.com", "tls": True}])

    assert _obtained(bench) == ["site-a1.zone.example"]


def test_a_plain_site_is_left_alone(tmp_path: Path) -> None:
    bench = _bench(tmp_path)
    bench.config.admin.tls = False
    _site(bench, "site-a1.zone.example", ssl=False)

    assert _obtained(bench) == []


def test_the_admin_certificate_still_follows_the_admin_flag(tmp_path: Path) -> None:
    bench = _bench(tmp_path)
    bench.config.admin.domain = "admin.example.com"

    bench.config.admin.tls = False
    assert "ADMIN" not in _obtained(bench)

    bench.config.admin.tls = True
    assert "ADMIN" in _obtained(bench)


def test_a_failure_names_the_certificate_domain_not_only_the_site(tmp_path: Path, capsys) -> None:
    """Certbot ran for the custom domain, so the report must name that domain."""
    from pilot.exceptions import CommandError

    bench = _bench(tmp_path)
    bench.config.admin.tls = False
    _site(bench, "site-a1.zone.example", ssl=False, domains=[{"domain": "shop.customer.com", "tls": True}])

    def _fail(self, site) -> None:
        raise CommandError("certbot failed")

    with patch.object(LetsEncryptManager, "obtain", _fail):
        LetsEncryptManager(bench).obtain_all()

    output = capsys.readouterr().out
    assert "'shop.customer.com'" in output
    assert "site 'site-a1.zone.example'" in output


@pytest.mark.parametrize(
    ("certificate_names", "reissues"),
    [
        ({"site.example.com", "shop.example.com"}, False),
        ({"site.example.com", "shop.example.com", "removed.example.com"}, True),
        ({"site.example.com"}, True),
    ],
)
def test_a_certificate_is_reissued_unless_it_names_exactly_the_site_domains(
    tmp_path: Path, certificate_names: set[str], reissues: bool
) -> None:
    from pilot.managers.nginx import NginxManager

    bench = _bench(tmp_path)
    bench.config.letsencrypt.email = "ops@example.com"
    site = SiteConfig(name="site.example.com", apps=[], ssl=True, domains=["shop.example.com"])

    with (
        patch.object(NginxManager, "has_cert", return_value=True),
        patch.object(LetsEncryptManager, "_is_near_expiry", return_value=False),
        patch("pilot.managers.letsencrypt.certificate_domain_names", return_value=certificate_names),
        patch("pilot.managers.letsencrypt.run_command") as certbot,
    ):
        LetsEncryptManager(bench).obtain(site)

    assert certbot.called is reissues


def test_certificate_domain_names_reads_only_the_dns_names(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from pilot.managers.letsencrypt import certificate_domain_names

    openssl_output = "X509v3 Subject Alternative Name: \n    DNS:erp.example.com, DNS:site1.example.com\n"
    with patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=openssl_output)):
        names = certificate_domain_names(tmp_path / "fullchain.pem")

    assert names == {"erp.example.com", "site1.example.com"}


def test_a_certificate_close_to_expiry_raises_an_alert(tmp_path: Path) -> None:
    from pilot.managers.nginx import NginxManager

    bench = _bench(tmp_path)
    _site(bench, "site.example.com", ssl=True)

    with (
        patch.object(NginxManager, "has_cert", return_value=True),
        patch("pilot.managers.letsencrypt.certificate_days_left", return_value=9),
        patch("pilot.core.notification.events.record_alert") as record_alert,
        patch("pilot.core.alerts.notify") as notify,
    ):
        LetsEncryptManager(bench).alert_expiring_certificates()

    payload = notify.call_args.args[1]
    assert payload["event"] == "certificate_expiry"
    assert payload["context"]["days_left"] == 9
    assert record_alert.call_args.kwargs["severity"] == "Warning"


def test_a_certificate_with_time_left_raises_nothing(tmp_path: Path) -> None:
    from pilot.managers.nginx import NginxManager

    bench = _bench(tmp_path)
    _site(bench, "site.example.com", ssl=True)

    with (
        patch.object(NginxManager, "has_cert", return_value=True),
        patch("pilot.managers.letsencrypt.certificate_days_left", return_value=40),
    ):
        assert LetsEncryptManager(bench).expiring_certificates() == []


def test_the_renewal_timer_is_installed_once_for_the_host(tmp_path: Path) -> None:
    from pilot.managers.environment import AdminEnvManager
    from pilot.managers.letsencrypt import install_renewal_timer, is_renewal_timer_installed

    root, unit_dir = tmp_path / "pilot", tmp_path / "systemd"
    with (
        patch("pilot.utils.cli_root", return_value=root),
        patch("pilot.managers.systemd_user.user_unit_dir", return_value=unit_dir),
        patch("pilot.managers.systemd_user.run_command") as run_command,
        patch.object(AdminEnvManager, "python", new=root / ".admin-venv" / "bin" / "python"),
    ):
        assert not is_renewal_timer_installed()
        install_renewal_timer()
        assert is_renewal_timer_installed()
        install_renewal_timer()

    service = (unit_dir / "pilot-certificates.service").read_text()
    assert f"ExecStart={root}/.admin-venv/bin/python -m pilot.managers.letsencrypt" in service
    assert (unit_dir / "pilot-certificates.timer").is_symlink()
    assert run_command.call_args.args[0][-3:] == ["enable", "--now", "pilot-certificates.timer"]


def _renewal_benches(*eligible: bool) -> list:
    from types import SimpleNamespace

    return [
        (
            Path(f"/benches/bench{index}"),
            SimpleNamespace(
                production=SimpleNamespace(enabled=True),
                letsencrypt=SimpleNamespace(email="ops@example.com" if is_eligible else ""),
            ),
        )
        for index, is_eligible in enumerate(eligible)
    ]


@pytest.mark.parametrize("renew_fails", [False, True])
def test_the_daily_renewal_renews_once_and_checks_every_bench(tmp_path: Path, renew_fails: bool) -> None:
    from pilot.exceptions import CommandError
    from pilot.managers import letsencrypt

    with (
        patch("pilot.utils.cli_root", return_value=tmp_path),
        patch("pilot.utils.iter_sibling_benches", return_value=_renewal_benches(True, False, True)),
        patch("pilot.core.bench.Bench", side_effect=lambda config, path: path),
        patch.object(letsencrypt, "LetsEncryptManager") as manager,
    ):
        if renew_fails:
            manager.return_value.renew.side_effect = CommandError("certbot failed")
        letsencrypt.main()

    benches = [call.args[0] for call in manager.call_args_list]
    assert benches == [Path("/benches/bench0"), Path("/benches/bench0"), Path("/benches/bench2")]
    assert manager.return_value.renew.call_count == 1
    assert manager.return_value.alert_expiring_certificates.call_count == 2
