import os
import tempfile

import pytest

from purchase_approval import ApprovalService


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "approval.db")


@pytest.fixture()
def svc(db_path):
    service = ApprovalService(db_path)
    yield service
    service.close()


@pytest.fixture()
def budgeted(svc):
    svc.define_budget("it", "2026Q4", 10_000_000)
    svc.define_budget("ops", "2026Q4", 10_000_000)
    return svc
