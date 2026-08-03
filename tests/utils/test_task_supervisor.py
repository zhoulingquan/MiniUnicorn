"""Tests for the background task supervisor."""

from __future__ import annotations

import asyncio
import logging

import pytest
from loguru import logger as loguru_logger

from miniunicorn.utils.task_supervisor import TaskSupervisor


@pytest.mark.asyncio
async def test_supervisor_logs_background_exception(caplog):
    supervisor = TaskSupervisor()

    async def fail():
        raise RuntimeError("background boom")

    handler_id = loguru_logger.add(caplog.handler, format="{message}", level="ERROR")
    try:
        with caplog.at_level(logging.ERROR):
            supervisor.create(fail(), name="failing-test")
            await supervisor.close(cancel=False, timeout_s=1)
    finally:
        loguru_logger.remove(handler_id)
    assert "failing-test" in caplog.text
    assert "background boom" in caplog.text


@pytest.mark.asyncio
async def test_close_can_cancel_and_drain_tasks():
    supervisor = TaskSupervisor()
    cancelled = asyncio.Event()

    async def wait_forever():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    supervisor.create(wait_forever(), name="wait-forever")
    await asyncio.sleep(0)
    await supervisor.close(cancel=True, timeout_s=1)
    assert cancelled.is_set()
    assert supervisor.pending_count == 0


@pytest.mark.asyncio
async def test_create_returns_named_task():
    supervisor = TaskSupervisor()

    async def ok():
        return 42

    task = supervisor.create(ok(), name="ok-task")
    assert task.get_name() == "ok-task"
    await supervisor.close(cancel=False, timeout_s=1)
    assert task.result() == 42


@pytest.mark.asyncio
async def test_pending_count_reflects_live_state():
    supervisor = TaskSupervisor()

    async def quick():
        await asyncio.sleep(0)

    assert supervisor.pending_count == 0
    task = supervisor.create(quick(), name="quick")
    assert supervisor.pending_count == 1
    await task
    # Done callback runs on the next loop iteration.
    await asyncio.sleep(0)
    assert supervisor.pending_count == 0
    await supervisor.close(cancel=False, timeout_s=1)


@pytest.mark.asyncio
async def test_close_with_no_tasks_is_noop():
    supervisor = TaskSupervisor()
    await supervisor.close(cancel=False, timeout_s=1)
    await supervisor.close(cancel=True, timeout_s=1)
    assert supervisor.pending_count == 0


@pytest.mark.asyncio
async def test_close_timeout_forces_cancellation():
    supervisor = TaskSupervisor()
    cancelled = asyncio.Event()

    async def hang():
        try:
            # Ignore cancellation once to force the timeout path.
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    supervisor.create(hang(), name="hang")
    await asyncio.sleep(0)
    # timeout_s=0 forces the gather to time out immediately, which then
    # cancels the still-pending task.
    await supervisor.close(cancel=True, timeout_s=0)
    assert cancelled.is_set()
    assert supervisor.pending_count == 0


@pytest.mark.asyncio
async def test_cancelled_task_is_not_logged_as_error(caplog):
    supervisor = TaskSupervisor()

    async def wait_forever():
        await asyncio.Event().wait()

    handler_id = loguru_logger.add(caplog.handler, format="{message}", level="ERROR")
    try:
        with caplog.at_level(logging.ERROR):
            supervisor.create(wait_forever(), name="cancel-only")
            await asyncio.sleep(0)
            await supervisor.close(cancel=True, timeout_s=1)
    finally:
        loguru_logger.remove(handler_id)
    failure_records = [
        r for r in caplog.records if "failed" in r.getMessage() and "cancel-only" in r.getMessage()
    ]
    assert not failure_records, (
        f"Cancelled task should not be logged as failure; got {[r.getMessage() for r in failure_records]}"
    )


@pytest.mark.asyncio
async def test_close_drains_already_completed_task_without_error():
    supervisor = TaskSupervisor()

    async def quick():
        return "done"

    supervisor.create(quick(), name="quick")
    # Let it finish before close.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await supervisor.close(cancel=False, timeout_s=1)
    assert supervisor.pending_count == 0
