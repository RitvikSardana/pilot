from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pilot.core.app import RevisionPin
from pilot.core.app.repository import AppRepository
from pilot.core.bench.rollout import AppRollout
from pilot.core.server import Server
from pilot.integrations.central import CentralClientError

STEPS = {"start_percent": 5, "step_percent": 5, "interval_hours": 8}


@pytest.fixture(autouse=True)
def cli_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("pilot.utils.cli_root", lambda: tmp_path)
    return tmp_path


def _rollout(tmp_path: Path) -> AppRollout:
    rollout = AppRollout(MagicMock(path=tmp_path, logs_path=tmp_path))
    rollout.settings = STEPS
    return rollout


def _settings_from_central(tmp_path: Path, reply: object, central_enabled: bool = True) -> dict | None:
    bench = MagicMock(path=tmp_path)
    bench.config.central.enabled = central_enabled
    with patch("pilot.integrations.central.client.CentralClient.config", return_value=reply):
        return AppRollout(bench).settings


def _set_rollout_number(cli_root: Path, number: int) -> None:
    (cli_root / "system").mkdir(exist_ok=True)
    (cli_root / "system" / "rollout-number").write_text(str(number))


def _move_first_seen_back(rollout: AppRollout, app_name: str, hours: int) -> None:
    clocks = json.loads(rollout.path.read_text())
    first_seen = datetime.fromisoformat(clocks[app_name]["first_seen"]) - timedelta(hours=hours)
    clocks[app_name]["first_seen"] = first_seen.isoformat()
    rollout.path.write_text(json.dumps(clocks))


def test_percent_grows_one_step_per_full_interval_up_to_100(tmp_path: Path) -> None:
    rollout = _rollout(tmp_path)
    now = datetime.now(UTC)

    assert rollout.get_percent(now) == 5
    assert rollout.get_percent(now - timedelta(hours=7)) == 5
    assert rollout.get_percent(now - timedelta(hours=8)) == 10
    assert rollout.get_percent(now - timedelta(hours=1000)) == 100
    assert rollout.get_percent(now + timedelta(hours=20)) == 5


def test_the_steps_come_from_central(tmp_path: Path) -> None:
    assert _settings_from_central(tmp_path, {"app_rollout": STEPS}) == STEPS


@pytest.mark.parametrize("reply", [{}, {"app_rollout": None}])
def test_a_central_without_rollout_steps_means_no_rollout(tmp_path: Path, reply: dict) -> None:
    assert _settings_from_central(tmp_path, reply) is None


@pytest.mark.parametrize(
    "steps",
    [
        {**STEPS, "step_percent": 0},
        {**STEPS, "interval_hours": "8"},
        {**STEPS, "interval_hours": True},
        {**STEPS, "start_percent": 101},
        {**STEPS, "step_percent": 101},
        {"start_percent": 5},
        "5,5,8",
    ],
)
def test_bad_steps_make_updates_wait(tmp_path: Path, steps: object) -> None:
    assert _settings_from_central(tmp_path, {"app_rollout": steps}) is False


@pytest.mark.parametrize("reply", [None, ["app_rollout"]])
def test_a_reply_that_is_not_an_object_makes_updates_wait(tmp_path: Path, reply: object) -> None:
    assert _settings_from_central(tmp_path, reply) is False


def test_every_update_is_offered_without_rollout_steps(tmp_path: Path, cli_root: Path) -> None:
    _set_rollout_number(cli_root, 100)
    rollout = AppRollout(MagicMock(path=tmp_path))
    rollout.settings = None

    assert rollout.is_offered("frappe", "abc", restart_on_new_target=True) is True
    assert not rollout.path.exists()


def test_a_bench_without_central_asks_central_nothing(tmp_path: Path) -> None:
    with patch("pilot.integrations.central.client.CentralClient.config") as config:
        assert _settings_from_central(tmp_path, {"app_rollout": STEPS}, central_enabled=False) is None

    config.assert_not_called()


def test_updates_wait_while_central_is_unreachable(tmp_path: Path, cli_root: Path) -> None:
    _set_rollout_number(cli_root, 1)
    bench = MagicMock(path=tmp_path)
    with patch(
        "pilot.integrations.central.client.CentralClient.config", side_effect=CentralClientError("down")
    ):
        assert AppRollout(bench).is_offered("frappe", "abc", restart_on_new_target=True) is False


def test_an_update_reaches_a_server_once_the_percent_passes_its_number(
    tmp_path: Path, cli_root: Path
) -> None:
    _set_rollout_number(cli_root, 37)
    rollout = _rollout(tmp_path)

    assert rollout.is_offered("frappe", "abc", restart_on_new_target=True) is False

    _move_first_seen_back(rollout, "frappe", hours=7 * 8)

    assert rollout.is_offered("frappe", "abc", restart_on_new_target=True) is True


def test_a_new_marketplace_release_starts_again_but_a_new_branch_commit_does_not(
    tmp_path: Path, cli_root: Path
) -> None:
    _set_rollout_number(cli_root, 37)
    rollout = _rollout(tmp_path)
    rollout.is_offered("frappe", "release-1", restart_on_new_target=True)
    rollout.is_offered("myapp", "commit-1", restart_on_new_target=False)
    _move_first_seen_back(rollout, "frappe", hours=100)
    _move_first_seen_back(rollout, "myapp", hours=100)

    assert rollout.is_offered("frappe", "release-2", restart_on_new_target=True) is False
    assert rollout.is_offered("myapp", "commit-2", restart_on_new_target=False) is True


def test_forget_removes_only_that_apps_clock(tmp_path: Path) -> None:
    rollout = _rollout(tmp_path)
    rollout.get_first_seen("frappe", "abc", restart_on_new_target=True)
    rollout.get_first_seen("erpnext", "def", restart_on_new_target=True)

    rollout.forget("frappe")

    assert set(json.loads(rollout.path.read_text())) == {"erpnext"}


def test_a_damaged_clock_file_starts_the_clock_again(tmp_path: Path) -> None:
    rollout = _rollout(tmp_path)
    rollout.path.write_text("{not json")

    first_seen = rollout.get_first_seen("frappe", "abc", restart_on_new_target=True)

    assert datetime.now(UTC) - first_seen < timedelta(minutes=1)


@pytest.mark.parametrize(
    "clock",
    [{"target": "abc"}, {"first_seen": "yesterday", "target": "abc"}, {"first_seen": 5}, "abc"],
)
def test_a_damaged_clock_entry_starts_again(tmp_path: Path, clock: object) -> None:
    rollout = _rollout(tmp_path)
    rollout.path.write_text(json.dumps({"frappe": clock}))

    first_seen = rollout.get_first_seen("frappe", "abc", restart_on_new_target=False)

    assert datetime.now(UTC) - first_seen < timedelta(minutes=1)
    assert json.loads(rollout.path.read_text())["frappe"]["first_seen"] == first_seen.isoformat()


def test_the_rollout_number_is_chosen_once(cli_root: Path) -> None:
    number = Server().get_rollout_number()

    assert 1 <= number <= 100
    assert Server().get_rollout_number() == number


@pytest.mark.parametrize("content", ["not a number", "0", "101", "-5"])
def test_a_damaged_rollout_number_is_replaced(
    cli_root: Path, content: str, capsys: pytest.CaptureFixture[str]
) -> None:
    path = cli_root / "system" / "rollout-number"
    path.parent.mkdir()
    path.write_text(content)

    number = Server().get_rollout_number()

    assert 1 <= number <= 100
    assert path.read_text() == str(number)
    assert "damaged rollout number" in capsys.readouterr().out


def _repository(newest: RevisionPin | None, on_revision: bool, is_offered: bool) -> AppRepository:
    app = MagicMock()
    app.config.name = "frappe"
    app.is_marketplace = False
    app.bench.app_rollout.is_offered.return_value = is_offered
    repository = AppRepository(app)
    repository.get_newest_target = MagicMock(return_value=newest)
    repository.is_on_revision = MagicMock(return_value=on_revision)
    return repository


def test_update_target_holds_back_an_update_the_rollout_has_not_offered() -> None:
    repository = _repository(RevisionPin(kind="commit", ref="abc"), on_revision=False, is_offered=False)

    assert repository.update_target() is None
    repository.app.bench.app_rollout.is_offered.assert_called_once_with("frappe", "abc", False)


def test_update_target_keeps_the_clock_when_the_lookup_finds_nothing() -> None:
    repository = _repository(None, on_revision=False, is_offered=True)

    assert repository.update_target() is None
    repository.app.bench.app_rollout.forget.assert_not_called()


def test_update_target_forgets_the_clock_of_an_app_that_is_up_to_date() -> None:
    pin = RevisionPin(kind="commit", ref="abc")
    repository = _repository(pin, on_revision=True, is_offered=False)

    assert repository.update_target() is pin
    repository.app.bench.app_rollout.forget.assert_called_once_with("frappe")
