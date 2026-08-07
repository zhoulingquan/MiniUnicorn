"""The only production Runtime Store façade (design §7.3, §8.4, §16).

One :class:`SqliteRuntimeStore` implements every narrow protocol view from
:mod:`miniunicorn.runtime.contracts`. The views exist for interface
segregation and tests; they share one connection factory, migration owner,
transaction policy, and database file (design §7.3).

The façade is a composition of responsibility mixins that each own a
single protocol slice (design §7.3, Task 12 Steps 3-5):

- :class:`BlobStoreMixin`           — ``BlobStore``
- :class:`TaskStoreMixin`           — ``TaskIngressStore`` + ``WorkerLedger``
- :class:`ExecutionStoreMixin`      — ``ExecutionJournal`` (model/tool/controls)
- :class:`SessionStoreMixin`        — ``SessionCommitLedger``
- :class:`OutboxStoreMixin`         — ``DeliveryLedger`` + durable reply read
- :class:`ResourceStoreMixin`       — ``ResourceLedger``
- :class:`MaintenanceStoreMixin`    — ``MaintenanceLedger`` + retention/backup
- :class:`SqliteStoreBase`          — connection, scope predicate, event log,
  lease validation, task-transition helper

Every mixin shares ``self._conn`` with the façade and calls shared helpers
on the base. No mixin imports another mixin (design §7.3). No method is
defined twice; the MRO is ordered so the base's ``__init__`` and shared
helpers win only when no responsibility mixin overrides them.

Safety invariants enforced here:

- every mutating Worker method validates ``task_id``, ``lease_token``, and
  ``lease_epoch`` before applying (design §6.10, §6.11);
- lease renewal does not increment ``state_version`` (design §6.11, §14.4);
- no SQLite transaction spans an external call (design §6.12);
- ``BEGIN IMMEDIATE`` is used for allocation, claim, reclaim, completion,
  and Outbox claim (design §16.1);
- state transitions are validated against :data:`TRANSITIONS` (design §14.2).
"""

from __future__ import annotations

from miniunicorn.runtime.sqlite.base_store import SqliteStoreBase
from miniunicorn.runtime.sqlite.blob_store import BlobStoreMixin
from miniunicorn.runtime.sqlite.execution_store import ExecutionStoreMixin
from miniunicorn.runtime.sqlite.maintenance_store import MaintenanceStoreMixin
from miniunicorn.runtime.sqlite.outbox_store import OutboxStoreMixin
from miniunicorn.runtime.sqlite.resource_store import ResourceStoreMixin
from miniunicorn.runtime.sqlite.session_store import SessionStoreMixin
from miniunicorn.runtime.sqlite.task_store import TaskStoreMixin

__all__ = ["SqliteRuntimeStore"]


class SqliteRuntimeStore(
    BlobStoreMixin,
    TaskStoreMixin,
    ExecutionStoreMixin,
    SessionStoreMixin,
    OutboxStoreMixin,
    ResourceStoreMixin,
    MaintenanceStoreMixin,
    SqliteStoreBase,
):
    """Single SQLite Runtime façade; mixins share one connection.

    The class body is intentionally empty. Every method lives in exactly
    one responsibility mixin (Task 12 Steps 3-5). Mixins are ordered so
    that protocol-specific methods take precedence over the shared base
    helpers, and the base's ``__init__``/connection/event/lease helpers
    are resolved last via the MRO.
    """
