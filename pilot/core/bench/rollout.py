from __future__ import annotations

import json
from datetime import UTC, datetime
from functools import cached_property
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pilot.core.bench import Bench
    from pilot.core.bench.migration.operation import AppRevision

# Datum rejects a log message over 8 KiB, and one character takes up to 4 bytes.
_LOG_MESSAGE_LIMIT = 2000


class AppRollout:
    """Offers each marketplace release to a share of servers that grows over time. Central
    owns the steps. The clock starts when the release was added to the registry, so every
    server reads the same time."""

    def __init__(self, bench: "Bench") -> None:
        self.bench = bench

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

        if not self._has_valid_steps(settings):
            print(f"App updates wait for Central: bad app rollout steps {settings!r}", flush=True)
            return False

        return settings

    def is_offered(self, app_name: str, commit: str) -> bool:
        """Whether the release has reached this server's rollout number."""
        from pilot.core.registry_cache import RegistryCache
        from pilot.core.server import Server
        from pilot.utils import cli_root

        if self.settings is None:
            return True

        if self.settings is False:
            return False

        added_at = RegistryCache(cli_root()).get_added_at(app_name, commit)
        if added_at is None:
            print(f"The {app_name} update waits: the registry does not say when it was added", flush=True)
            return False

        return Server().get_rollout_number() <= self.get_percent(added_at)

    def get_percent(self, added_at: datetime) -> int:
        """The start percent plus one step for each full interval since the release was added."""
        settings = self.settings
        assert settings, "Only called when Central sent rollout steps."

        hours = (datetime.now(UTC) - added_at).total_seconds() / 3600
        # A clock set back in time must not drop below the start percent.
        steps = max(0, int(hours // settings["interval_hours"]))
        return min(100, settings["start_percent"] + steps * settings["step_percent"])

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

    @staticmethod
    def _has_valid_steps(settings: object) -> bool:
        """Every step is a whole number of at least 1, and the percents are at most 100."""
        if not isinstance(settings, dict):
            return False

        values = [settings.get(key) for key in ("start_percent", "step_percent", "interval_hours")]
        # JSON true passes isinstance(value, int), so check the exact type.
        if not all(type(value) is int and value >= 1 for value in values):
            return False

        return settings["start_percent"] <= 100 and settings["step_percent"] <= 100
