from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from pilot.commands.apps.uninstall import UninstallAppCommand
from tests.pilot.commands.test_commands import make_bench


@pytest.mark.parametrize(("keep_on_bench", "remove_from_bench"), [(False, True), (True, False)])
def test_uninstall_app_removes_an_unused_app_from_the_bench_unless_kept(
    tmp_path: Path, keep_on_bench: bool, remove_from_bench: bool
) -> None:
    bench = make_bench(tmp_path)

    with patch("pilot.core.site.Site.uninstall_apps") as uninstall_apps:
        UninstallAppCommand(bench, site_name="site1.localhost", app_names=["myapp"], keep_on_bench=keep_on_bench).run()

    assert uninstall_apps.call_args.kwargs["remove_from_bench"] is remove_from_bench
