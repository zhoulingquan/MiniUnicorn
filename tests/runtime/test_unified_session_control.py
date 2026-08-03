"""Durable root-task cancellation tests for unified session mode.

Verifies that a CANCEL control request submitted through the TaskService
is persisted in the Runtime Store and transitions a queued task to
CANCELLED, all under the unified session key.
"""

import pytest

from miniunicorn.agent.loop import UNIFIED_SESSION_KEY
from miniunicorn.runtime.models import TaskControlRequest
from miniunicorn.runtime.task_service import TaskService


@pytest.mark.asyncio
async def test_unified_session_root_task_cancel_is_durable(
    store, sample_scope, make_inbound_envelope
):
    """A CANCEL control request on a queued unified-session task is durable."""
    service = TaskService(store)
    envelope = make_inbound_envelope(
        sample_scope,
        session_key=UNIFIED_SESSION_KEY,
        channel_message_id="unified-cancel-1",
    )
    handle = await service.submit(envelope)
    result = await service.control(
        TaskControlRequest(
            task_id=handle.task_id,
            kind="CANCEL",
            dedup_key="cancel-unified-1",
            payload_blob_id=None,
            requested_by="test-user",
            requested_at_ms=1_000_002,
        )
    )
    assert result.status == "APPENDED"
    task = store.read_task(handle.task_id)
    assert task is not None
    assert task.session_key == UNIFIED_SESSION_KEY
    assert task.state == "CANCELLED"
