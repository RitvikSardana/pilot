from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from pilot.core.bench.telemetry import DatumCredential
from pilot.core.credential import RotatingCredential, get_jwt_expiry
from tests.pilot.commands.test_setup_production import _make_bench
from tests.pilot.core.test_bench_telemetry import _enrol_with_central

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def _jwt(expires_at: datetime | None) -> str:
    claims = {"sub": "pilot"} if expires_at is None else {"exp": int(expires_at.timestamp())}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{payload}.signature"


class FakeCredential(RotatingCredential):
    rotate_before_seconds = 3600

    def __init__(self, token: str, fresh_token: str) -> None:
        super().__init__(token)
        self.fresh_token = fresh_token

    def rotate(self) -> str:
        return self.fresh_token


def test_jwt_expiry_reads_the_exp_claim() -> None:
    assert get_jwt_expiry(_jwt(NOW)) == NOW


def test_jwt_expiry_is_none_for_a_token_it_cannot_read() -> None:
    assert get_jwt_expiry(_jwt(None)) is None
    assert get_jwt_expiry("not-a-jwt") is None
    assert get_jwt_expiry("") is None


def test_a_fresh_token_is_not_expiring() -> None:
    credential = FakeCredential(_jwt(NOW + timedelta(hours=2)), "new")
    assert not credential.is_expiring(now=NOW)


def test_a_token_inside_the_window_or_past_it_is_expiring() -> None:
    assert FakeCredential(_jwt(NOW + timedelta(minutes=30)), "new").is_expiring(now=NOW)
    assert FakeCredential(_jwt(NOW - timedelta(minutes=1)), "new").is_expiring(now=NOW)


def test_a_token_without_a_known_expiry_is_expiring() -> None:
    assert FakeCredential("", "new").is_expiring(now=NOW)


def test_rotate_if_expiring_leaves_a_fresh_token_alone() -> None:
    token = _jwt(NOW + timedelta(hours=2))
    credential = FakeCredential(token, "new")

    assert not credential.rotate_if_expiring(now=NOW)
    assert credential.token == token


def test_rotate_if_expiring_replaces_an_expiring_token() -> None:
    credential = FakeCredential(_jwt(NOW), "new")

    assert credential.rotate_if_expiring(now=NOW)
    assert credential.token == "new"


def test_rotate_if_expiring_reports_no_change_when_the_issuer_returns_the_same_token() -> None:
    token = _jwt(NOW)
    credential = FakeCredential(token, token)

    assert not credential.rotate_if_expiring(now=NOW)


def test_datum_credential_rotates_from_central(tmp_path: Path) -> None:
    bench = _make_bench(tmp_path)
    _enrol_with_central(bench)
    fresh = _jwt(datetime.now(UTC) + timedelta(days=7))

    with patch("pilot.integrations.central.CentralClient") as client_cls:
        client_cls.return_value.datum_token.return_value = {
            "token": fresh,
            "endpoint": "https://datum.region.test",
        }
        assert DatumCredential(bench, _jwt(NOW)).rotate_if_expiring()

    assert bench.config.telemetry.token == fresh
    assert bench.config.telemetry.endpoint == "https://datum.region.test"


def test_datum_credential_keeps_the_old_token_when_central_fails(tmp_path: Path) -> None:
    from pilot.integrations.central.client import CentralClientError

    bench = _make_bench(tmp_path)
    _enrol_with_central(bench)
    old = _jwt(NOW)
    bench.config.telemetry.token = old
    messages: list[str] = []

    with patch("pilot.integrations.central.CentralClient") as client_cls:
        client_cls.return_value.datum_token.side_effect = CentralClientError("down")
        credential = DatumCredential(bench, old, on_progress=messages.append)
        assert not credential.rotate_if_expiring()

    assert bench.config.telemetry.token == old
    assert messages == ["Could not fetch a Datum token from Central: down"]
