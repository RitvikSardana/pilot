"""Tests for the site-down incident alerts raised by the uptime monitor."""

import json
from pathlib import Path
from unittest.mock import patch

from pilot.config import BenchConfig, MariaDBConfig, RedisConfig, WorkerConfig
from pilot.core.alerts import ALERT_SUSTAINED_SECONDS
from pilot.core.bench import Bench
from pilot.core.site.uptime_monitoring import UptimeMonitor


def _monitor(tmp_path: Path, *, alerting: bool = True, webhooks: dict | None = None) -> UptimeMonitor:
    config = BenchConfig(
        name="my-bench",
        python_version="3.14",
        mariadb=MariaDBConfig(),
        redis=RedisConfig(),
        workers=WorkerConfig(),
    )
    config.resource_limits.site_uptime = alerting
    config.resource_limits.webhook_endpoints = webhooks or {"https://alerts.example.com": "tok"}
    monitor = UptimeMonitor(Bench(config, tmp_path / "my-bench"))
    monitor.alerts_path.parent.mkdir(parents=True, exist_ok=True)
    return monitor


def _ping(site: str, up: bool) -> dict:
    return {
        "time": "2026-08-10T12:00:00+00:00",
        "site": site,
        "up": up,
        "status_code": 200 if up else 502,
        "response_ms": 12,
    }


def _age_alerts(monitor: UptimeMonitor) -> None:
    state = json.loads(monitor.alerts_path.read_text())
    for entry in state.values():
        entry["since"] -= ALERT_SUSTAINED_SECONDS + 1
    monitor.alerts_path.write_text(json.dumps(state))


def test_a_single_failed_ping_is_not_an_incident(tmp_path: Path) -> None:
    monitor = _monitor(tmp_path)
    sent = []

    with patch("pilot.core.alerts.send_alert", lambda *args: sent.append(args)):
        monitor.send_alert_if_required([_ping("a.test", up=False)])

    assert sent == []
    assert list(json.loads(monitor.alerts_path.read_text())) == ["a.test"]


def test_a_site_down_for_the_whole_window_alerts_once(tmp_path: Path) -> None:
    monitor = _monitor(tmp_path)
    monitor.send_alert_if_required([_ping("a.test", up=False)])
    _age_alerts(monitor)
    payloads = []

    with patch("pilot.core.alerts.send_alert", lambda endpoint, token, payload: payloads.append(payload)):
        monitor.send_alert_if_required([_ping("a.test", up=False)])
        monitor.send_alert_if_required([_ping("a.test", up=False)])

    assert len(payloads) == 1
    assert payloads[0]["event"] == "site_down"
    assert payloads[0]["message"] == "my-bench: a.test unreachable"
    assert payloads[0]["context"]["sites"] == [
        {"site": "a.test", "status_code": 502, "response_ms": 12, "error": None}
    ]


def test_only_the_sites_still_down_are_reported(tmp_path: Path) -> None:
    monitor = _monitor(tmp_path)
    monitor.send_alert_if_required([_ping("a.test", up=False), _ping("b.test", up=False)])
    _age_alerts(monitor)
    payloads = []

    with patch("pilot.core.alerts.send_alert", lambda endpoint, token, payload: payloads.append(payload)):
        monitor.send_alert_if_required([_ping("a.test", up=False), _ping("b.test", up=True)])

    assert [site["site"] for site in payloads[0]["context"]["sites"]] == ["a.test"]
    assert list(json.loads(monitor.alerts_path.read_text())) == ["a.test"]


def test_every_site_recovering_removes_the_alerts_file(tmp_path: Path) -> None:
    monitor = _monitor(tmp_path)
    monitor.send_alert_if_required([_ping("a.test", up=False)])

    monitor.send_alert_if_required([_ping("a.test", up=True)])

    assert not monitor.alerts_path.exists()


def test_alerts_go_only_to_the_configured_endpoints(tmp_path: Path) -> None:
    monitor = _monitor(tmp_path, webhooks={"https://one.test": "a", "https://two.test": "b"})
    monitor.send_alert_if_required([_ping("a.test", up=False)])
    _age_alerts(monitor)
    reached = []

    with patch("pilot.core.alerts.send_alert", lambda endpoint, token, payload: reached.append(endpoint)):
        monitor.send_alert_if_required([_ping("a.test", up=False)])

    assert reached == ["https://one.test", "https://two.test"]


def test_no_alerts_when_uptime_alerting_is_switched_off(tmp_path: Path) -> None:
    monitor = _monitor(tmp_path, alerting=False)
    sent = []

    with patch("pilot.core.alerts.send_alert", lambda *args: sent.append(args)):
        monitor.send_alert_if_required([_ping("a.test", up=False)])
        _age = monitor.alerts_path.exists()
        monitor.send_alert_if_required([_ping("a.test", up=False)])

    assert sent == []
    assert not _age, "a disabled alert keeps no window state to go stale"


def test_collect_logs_every_ping_and_then_alerts(tmp_path: Path) -> None:
    monitor = _monitor(tmp_path)
    monitor._configurator.log_path.parent.mkdir(parents=True, exist_ok=True)

    with (
        patch.object(UptimeMonitor, "get_sites", return_value=["a.test"]),
        patch.object(UptimeMonitor, "ping_site", return_value=_ping("a.test", up=False)),
    ):
        monitor.collect()

    logged = json.loads(monitor._configurator.log_path.read_text().splitlines()[-1])
    assert logged["site"] == "a.test"
    assert list(json.loads(monitor.alerts_path.read_text())) == ["a.test"]


def test_an_undelivered_alert_is_retried_on_the_next_tick(tmp_path: Path) -> None:
    """Every sink being down is not a delivered alert, so the incident must not
    be marked notified and silently forgotten."""
    monitor = _monitor(tmp_path)
    monitor.send_alert_if_required([_ping("a.test", up=False)])
    _age_alerts(monitor)

    def refuse(endpoint, token, payload):
        raise OSError("connection refused")

    with patch("pilot.core.alerts.send_alert", refuse):
        monitor.send_alert_if_required([_ping("a.test", up=False)])

    assert json.loads(monitor.alerts_path.read_text())["a.test"]["notified"] is False

    delivered = []
    with patch("pilot.core.alerts.send_alert", lambda endpoint, token, payload: delivered.append(endpoint)):
        monitor.send_alert_if_required([_ping("a.test", up=False)])

    assert delivered == ["https://alerts.example.com"]
    assert json.loads(monitor.alerts_path.read_text())["a.test"]["notified"] is True


def test_a_site_that_stays_down_is_recorded_locally_once(tmp_path: Path) -> None:
    """The retry above repeats every five seconds. Only the delivery repeats -
    the bench's own feed keeps one row for the incident."""
    monitor = _monitor(tmp_path)
    monitor.send_alert_if_required([_ping("a.test", up=False)])
    _age_alerts(monitor)

    def refuse(endpoint, token, payload):
        raise OSError("connection refused")

    with patch("pilot.core.alerts.send_alert", refuse):
        for _ in range(5):
            monitor.send_alert_if_required([_ping("a.test", up=False)])

    recorded = monitor.bench.notifications.list(10)
    assert [item.title for item in recorded] == ["a.test is unreachable"]


def test_a_second_site_going_down_gets_its_own_record(tmp_path: Path) -> None:
    """Recording is per condition, so a new site joining an existing incident is
    not swallowed by the first site's record."""
    monitor = _monitor(tmp_path)
    monitor.send_alert_if_required([_ping("a.test", up=False)])
    _age_alerts(monitor)

    with patch("pilot.core.alerts.send_alert", lambda endpoint, token, payload: None):
        monitor.send_alert_if_required([_ping("a.test", up=False)])
        monitor.send_alert_if_required([_ping("a.test", up=False), _ping("b.test", up=False)])
        _age_alerts(monitor)
        monitor.send_alert_if_required([_ping("a.test", up=False), _ping("b.test", up=False)])

    titles = sorted(item.title for item in monitor.bench.notifications.list(10))
    assert titles == ["a.test is unreachable", "b.test is unreachable"]


def test_a_ping_without_an_http_answer_keeps_its_reason(tmp_path: Path) -> None:
    import urllib.error

    reason = "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError(reason)):
        result = _monitor(tmp_path).ping_site("a.test")

    assert result["up"] is False
    assert result["status_code"] is None
    assert result["error"] == reason


def test_an_http_failure_has_no_extra_error_field(tmp_path: Path) -> None:
    import urllib.error

    failure = urllib.error.HTTPError("https://a.test/api/method/ping", 502, "Bad Gateway", {}, None)
    with patch("urllib.request.urlopen", side_effect=failure):
        result = _monitor(tmp_path).ping_site("a.test")

    assert result["status_code"] == 502
    assert "error" not in result
