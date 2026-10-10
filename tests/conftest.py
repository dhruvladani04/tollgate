import pytest

from tollgate.approve.queue import APPROVALS_FILE, ApprovalQueue
from tollgate.audit.store import AuditStore


@pytest.fixture
def audit(tmp_path):
    with AuditStore(tmp_path / "audit.sqlite") as store:
        yield store


@pytest.fixture
def approvals(tmp_path):
    with ApprovalQueue(tmp_path / APPROVALS_FILE, poll_seconds=0.01) as queue:
        yield queue
