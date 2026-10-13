from __future__ import annotations

import json
from datetime import UTC, datetime
from functools import cached_property
from typing import TYPE_CHECKING, Literal

from pilot.internal.atomic_file import exclusive_file_lock, replace_private_text_locked

if TYPE_CHECKING:
    from pilot.core.bench import Bench
    from pilot.core.bench.migration.operation import AppRevision

# Datum rejects a log message over 8 KiB, and one character takes up to 4 bytes.
_LOG_MESSAGE_LIMIT = 2000


class AppRollout:
    """Offers each pending app update to a share of servers that grows over time. Central
    owns the steps. `app-rollout.json` holds when this bench first saw each update."""

    def __init__(self, bench: "Bench") -> None:
        self.bench = bench
        self.path = bench.path / "app-rollout.json"

    @cached_property
    def settings(self) -> dict | None | Literal[False]:
        """Central's `start_percent`, `step_percent` and `interval_hours`. None when there is
        no rollout and every update is offered at once. False when Central cannot answer or
        sends bad steps, so new updates wait for it."""
        from pilot.integrations.central.client import CentralClient, CentralClientError

        if not self.bench.config.central.enabled:
            return None

        try:
            reply = CentralClient().config()
        except CentralClientError as error:
            print(f"App updates wait for Central: {error}", flush=True)
            return False

        if not isinstance(reply, dict):
            print(f"App updates wait for Central: bad reply {reply!r}", flush=True)
            return False

        settings = reply.get("app_rollout")
        if settings is None:
            return None

        for key in ("start_percent", "step_percent", "interval_hours"):
            value = settings.get(key) if isinstance(settings, dict) else None
            if not isinstance(value, int) or value < 1:
                print(f"App updates wait for Central: bad app rollout steps {settings!r}", flush=True)
                return False

        return settings

    def is_offered(self, app_name: str, target: str, restart_on_new_target: bool) -> bool:
        """Whether the update has reached this server's rollout number."""
        from pilot.core.server import Server

        if self.settings is None:
            return True

        if self.settings is False:
            return False

        first_seen = self.get_first_seen(app_name, target, restart_on_new_target)
        return Server().get_rollout_number() <= self.get_percent(first_seen)

    def get_first_seen(self, app_name: str, target: str, restart_on_new_target: bool) -> datetime:
        """When this bench first saw the app's pending update. A branch app keeps its first
        time when new commits arrive, or a busy branch would never reach every server."""
        with exclusive_file_lock(self.path):
            clocks = self._read()
            clock = clocks.get(app_name)
            try:
                first_seen = datetime.fromisoformat(clock["first_seen"]).astimezone(UTC)
                keeps_clock = not restart_on_new_target or clock.get("target") == target
            except (TypeError, KeyError, ValueError):
                # A missing or damaged clock starts again, which only delays the update.
                keeps_clock = False

            if not keeps_clock:
                first_seen = datetime.now(UTC)
                clocks[app_name] = {"first_seen": first_seen.isoformat(), "target": target}
                replace_private_text_locked(self.path, json.dumps(clocks, indent=2))

        return first_seen

    def get_percent(self, first_seen: datetime) -> int:
        """The start percent plus one step for each full interval since `first_seen`."""
        hours = (datetime.now(UTC) - first_seen).total_seconds() / 3600
        # A clock set back in time must not drop below the start percent.
        steps = max(0, int(hours // self.settings["interval_hours"]))
        return min(100, self.settings["start_percent"] + steps * self.settings["step_percent"])

    def forget(self, app_name: str) -> None:
        """Remove the app's clock when it is up to date, so its next update starts at the start percent."""
        if not self.path.exists():
            return

        with exclusive_file_lock(self.path):
            clocks = self._read()
            if clocks.pop(app_name, None) is not None:
                replace_private_text_locked(self.path, json.dumps(clocks, indent=2))

    def log_updates(
        self, revisions: list["AppRevision"], status: Literal["success", "failed"], error: str = ""
    ) -> None:
        """One line per updated app in `logs/app_updates.json.log`, which fluent-bit ships to Datum."""
        from pilot.core.server import Server

        try:
            rollout_number = Server().get_rollout_number()
            with (self.bench.logs_path / "app_updates.json.log").open("a") as log_file:
                for revision in revisions:
                    line = {
                        "time": datetime.now(UTC).isoformat(),
                        "level": "info" if status == "success" else "error",
                        "message": error[:_LOG_MESSAGE_LIMIT] or status,
                        "status": status,
                        "app": revision.name,
                        "from_commit": revision.sha,
                        "to_commit": revision.target_sha,
                        "rollout_number": rollout_number,
                    }
                    log_file.write(json.dumps(line) + "\n")
        except OSError as log_error:
            print(f"Could not write the app update log: {log_error}", flush=True)

    def _read(self) -> dict:
        """A missing or damaged file starts every clock again, which only delays updates."""
        try:
            clocks = json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            return {}

        return clocks if isinstance(clocks, dict) else {}
