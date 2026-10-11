import socket
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from pilot.core.bench.cloning import runtime


def test_temporary_redis_owner_waits_for_other_clone(source, monkeypatch):
    if not runtime.redis_server_binary():
        pytest.skip("Redis server unavailable")
    with socket.socket() as cache, socket.socket() as queue:
        cache.bind(("127.0.0.1", 0))
        queue.bind(("127.0.0.1", 0))
        source.config.redis.cache_port = cache.getsockname()[1]
        source.config.redis.queue_port = queue.getsockname()[1]
    owner_ready = Event()
    borrower_ready = Event()
    finish_owner = Event()
    finish_borrower = Event()
    shutdown_waiting = Event()
    flock = runtime.fcntl.flock
    processes = []
    popen = runtime.subprocess.Popen

    def observe_lock(descriptor, operation):
        if isinstance(descriptor, int) and operation == runtime.fcntl.LOCK_EX:
            shutdown_waiting.set()
        return flock(descriptor, operation)

    def spawn(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(runtime.fcntl, "flock", observe_lock)
    monkeypatch.setattr(runtime.subprocess, "Popen", spawn)

    def clone(ready, finish):
        with runtime.ForkRuntime(source, allow_existing=True):
            ready.set()
            assert finish.wait(10)

    ports = (source.config.redis.cache_port, source.config.redis.queue_port)
    with ThreadPoolExecutor(max_workers=2) as executor:
        owner = executor.submit(clone, owner_ready, finish_owner)
        borrower = None
        try:
            if not owner_ready.wait(5):
                owner.result(timeout=1)
                pytest.fail("Owner runtime did not become ready")
            borrower = executor.submit(clone, borrower_ready, finish_borrower)
            assert borrower_ready.wait(5)
            finish_owner.set()
            assert shutdown_waiting.wait(5)
            assert not owner.done()
            assert all(runtime.ForkRuntime.has_existing_redis(port) for port in ports)
            assert len(processes) == 2
        finally:
            finish_owner.set()
            finish_borrower.set()
        owner.result(timeout=10)
        if borrower is not None:
            borrower.result(timeout=10)
    assert all(process.poll() is not None for process in processes)
    assert not any(runtime.ForkRuntime.has_existing_redis(port) for port in ports)
