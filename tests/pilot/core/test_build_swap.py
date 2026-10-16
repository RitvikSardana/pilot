from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pilot.core import build_memory


def _host(tmp_path: Path, available_mb: int, helper: bool = True):
    helper_path = tmp_path / "pilot-build-swap"
    if helper:
        helper_path.write_text("#!/bin/sh\n")
    return (
        patch.object(build_memory, "BUILD_SWAP_HELPER", helper_path),
        patch.object(build_memory, "can_read_memory", return_value=True),
        patch.object(build_memory, "available_memory_mb", return_value=available_mb),
    )


def test_a_small_host_gets_swap_for_the_length_of_the_build(tmp_path: Path) -> None:
    helper, readable, available = _host(tmp_path, available_mb=1500)
    succeeded = SimpleNamespace(returncode=0, stderr="")
    with (
        helper,
        readable,
        available,
        patch("subprocess.run", return_value=succeeded) as run,
        build_memory.build_swap(),
    ):
        assert [call.args[0][-1] for call in run.call_args_list] == ["on"]

    assert [call.args[0][-1] for call in run.call_args_list] == ["on", "off"]
    assert run.call_args.args[0][:2] == ["sudo", "-n"]


@pytest.mark.parametrize(("available_mb", "helper_installed"), [(8000, True), (1500, False)])
def test_no_swap_is_added_with_enough_memory_or_without_the_helper(
    tmp_path: Path, available_mb: int, helper_installed: bool
) -> None:
    helper, readable, available = _host(tmp_path, available_mb, helper=helper_installed)
    with helper, readable, available, patch("subprocess.run") as run, build_memory.build_swap():
        pass

    run.assert_not_called()


def test_a_helper_that_fails_never_stops_the_build_or_turns_swap_off(tmp_path: Path) -> None:
    helper, readable, available = _host(tmp_path, available_mb=1500)
    failed = SimpleNamespace(returncode=1, stderr="sudo: a password is required")
    with helper, readable, available, patch("subprocess.run", return_value=failed) as run, build_memory.build_swap():
        pass

    assert [call.args[0][-1] for call in run.call_args_list] == ["on"]
