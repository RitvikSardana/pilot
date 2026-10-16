"""Apply a retention policy to one site's backups, local and offsite."""

import re
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from pilot.config.site_backup import clear_retention, read_retention, write_retention
from pilot.core.site.retention import BackupRetentionPolicy
from pilot.integrations.s3.backups import OffsiteBackup
from pilot.internal.atomic_file import exclusive_file_lock

if TYPE_CHECKING:
    from pilot.core.site import Site

_TS_RE = re.compile(r"^(\d{8}_\d{6})")


def parse_backup_timestamp(filename: str) -> str | None:
    """Return a Frappe backup filename's YYYYMMDD_HHMMSS prefix."""
    match = _TS_RE.match(filename)
    return match.group(1) if match else None


class SiteBackups:
    def __init__(self, site: "Site") -> None:
        self.site = site

    @property
    def directory(self) -> Path:
        """Outside private/backups, which Frappe prunes by age and by its backup limit."""
        return self.site.path / "backups"

    @property
    def runs_lock_path(self) -> Path:
        """Held while a run moves in or retention deletes runs."""
        return self.site.path / "backup_runs"

    def take(self, with_files: bool = True) -> tuple[str, list[Path]]:
        """Run `frappe backup` in its own staging directory and return the run it made."""
        import tempfile

        from pilot.exceptions import BenchError
        from pilot.utils import run_command

        self.directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".backup-", dir=self.directory) as staging:
            argv = [
                *self.site.bench.frappe_call,
                "frappe",
                "--site",
                self.site.config.name,
                "backup",
                "--backup-path",
                staging,
            ]
            if with_files:
                argv.append("--with-files")
            # cwd matters: frappe's bench helper reads apps.txt from the current dir.
            run_command(argv, cwd=self.site.bench.sites_path, stream_output=True)
            files = self._publish(sorted(Path(staging).iterdir()))
        timestamp = parse_backup_timestamp(files[0].name) if files else None
        if not timestamp:
            raise BenchError(f"The backup of {self.site.config.name} produced no files.")
        return timestamp, files

    def _publish(self, staged: list[Path]) -> list[Path]:
        """Move a run in under the backups lock, so retention sees all of it or none of it."""
        from pilot.exceptions import BenchError

        with exclusive_file_lock(self.runs_lock_path):
            if any((self.directory / path.name).exists() for path in staged):
                raise BenchError(
                    f"A backup of {self.site.config.name} from the same second already exists. Try again."
                )
            return [path.rename(self.directory / path.name) for path in staged]

    def fetch_run(self, timestamp: str, destination: Path) -> list[Path]:
        """One run's files on local disk, downloaded into `destination` when only the
        offsite copy is left."""
        from pilot.exceptions import BenchError

        local = sorted(path for path in self.directory.glob(f"{timestamp}-*") if path.is_file())
        if local:
            return local
        offsite = self._offsite()
        files = offsite.get_backup(self.site.config.name, timestamp) if offsite else None
        if offsite is None or not files:
            raise BenchError(f"Backup {timestamp} of {self.site.config.name} was not found.")
        paths = []
        for filename in files.values():
            offsite.download(self.site.config.name, timestamp, filename, destination / filename)
            paths.append(destination / filename)
        return paths

    def prune(self) -> list[str]:
        """Delete runs rejected by this site's retention policy."""
        config = read_retention(self.site.path / "site_config.json")
        if config is None:
            return []

        offsite = self._offsite()
        offsite_runs = offsite.list_backups(self.site.config.name) if offsite else {}
        policy = BackupRetentionPolicy(config)
        with exclusive_file_lock(self.runs_lock_path):
            timestamps = sorted(set(self._local_timestamps()) | set(offsite_runs))
            selected = policy.select_deletions(timestamps)

        # Offsite deletes are network calls, so publishing does not wait on them.
        deleted = [ts for ts in selected if self._delete_offsite_run(offsite, offsite_runs, ts)]
        with exclusive_file_lock(self.runs_lock_path):
            for timestamp in deleted:
                self._delete_local(timestamp)
        return deleted

    def resolve_file(self, filename: str) -> Path:
        """A path inside this site's own backup directory. These names arrive from API
        requests, so anything that would reach outside it raises instead."""
        from pilot.exceptions import BenchError

        if not filename or "/" in filename or "\\" in filename or filename.startswith("."):
            raise BenchError("Backup filename is invalid.")
        backups_dir = self.directory.resolve()
        target = (backups_dir / filename).resolve()
        if backups_dir not in target.parents:
            raise BenchError("Backup filename is invalid.")
        return target

    def download_file_path(self, timestamp: str, file_id: str) -> Path:
        from pilot.exceptions import BenchError

        if not file_id.startswith(timestamp):
            raise BenchError("Backup filename is invalid.")
        target = self.resolve_file(file_id)
        if not target.is_file():
            raise FileNotFoundError(file_id)
        return target

    def download_links(self, timestamp: str) -> dict:
        offsite = OffsiteBackup.from_config(self.site.bench.config.s3, self.site.bench.path)
        files = offsite.get_backup(self.site.config.name, timestamp)
        if not files:
            raise FileNotFoundError(timestamp)
        return {
            kind: offsite.presigned_url(self.site.config.name, timestamp, filename)
            for kind, filename in files.items()
        }

    def schedule(self) -> dict:
        from pilot.managers.cron import CronManager

        schedule = CronManager(self.site.bench.path).get_schedule(self.site.config.name)
        retention = read_retention(self.site.path / "site_config.json")
        return {"schedule": schedule, "retention": asdict(retention) if retention else None}

    def set_schedule(self, schedule: str, retention) -> dict:
        from pilot.managers.cron import CronManager

        CronManager(self.site.bench.path).set_schedule(
            self.site.config.name,
            schedule,
            self._cron_command(),
        )
        write_retention(self.site.path / "site_config.json", retention)
        return self.schedule()

    def move_schedule(self, old_name: str) -> None:
        """Carry a renamed site's backup schedule over from its old name."""
        from pilot.managers.cron import CronManager

        CronManager(self.site.bench.path).move_schedule(old_name, self.site.config.name, self._cron_command())

    def clear_schedule(self) -> None:
        from pilot.managers.cron import CronManager

        CronManager(self.site.bench.path).remove_schedule(self.site.config.name)
        clear_retention(self.site.path / "site_config.json")

    def retention_from_payload(self, block: dict | None):
        return retention_from_payload(block)

    def _delete_offsite_run(self, offsite, offsite_runs: dict, timestamp: str) -> bool:
        """Delete one run offsite. False when that fails, so its local files stay."""
        if timestamp in offsite_runs:
            try:
                self._delete_offsite(offsite, timestamp, offsite_runs[timestamp])
            except Exception as error:
                print(f"Kept backup {timestamp}: offsite delete failed: {error}")
                return False
        return True

    def _local_timestamps(self) -> list[str]:
        if not self.directory.is_dir():
            return []
        return [
            timestamp
            for file in self.directory.iterdir()
            if file.is_file() and (timestamp := parse_backup_timestamp(file.name))
        ]

    def _delete_local(self, timestamp: str) -> None:
        if not self.directory.is_dir():
            return
        for file in self.directory.glob(f"{timestamp}-*"):
            file.unlink(missing_ok=True)

    def _delete_offsite(self, offsite: OffsiteBackup, timestamp: str, files: dict[str, str]) -> None:
        for filename in list(files.values()):
            offsite.delete(self.site.config.name, timestamp, filename)

    def _offsite(self) -> OffsiteBackup | None:
        if not self.site.bench.config.s3.is_configured:
            return None
        return OffsiteBackup.from_config(self.site.bench.config.s3, self.site.bench.path)

    def _cron_command(self) -> str:
        from pilot.managers.cron import cron_module_command

        return cron_module_command(
            "pilot.tasks.backup_site",
            [self.site.bench.path, self.site.config.name, "--with-files"],
            self.site.bench.logs_path / f"backup-{self.site.config.name}.log",
        )


def retention_from_payload(block: dict | None):
    from pilot.config import VALID_SCHEMES, BackupConfig

    block = block or {}
    config = BackupConfig()
    scheme = str(block.get("scheme", config.scheme)).strip()
    if scheme not in VALID_SCHEMES:
        return f"Retention scheme must be one of: {', '.join(VALID_SCHEMES)}."
    config.scheme = scheme
    for key in config.counts:
        if key not in block:
            continue
        try:
            value = int(block[key])
        except (TypeError, ValueError):
            return f"{key} must be a whole number."
        if value < 0:
            return f"{key} must be zero or more."
        setattr(config, key, value)
    return config
