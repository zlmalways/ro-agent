"""Repeat the retained lock-loss regression ten times, matching Mac's stated check."""
import pytest

from tests.integration.test_runtime_owner import owner_options  # noqa: F401
from tests.integration.test_runtime_owner import test_db_lock_loss_disables_old_process as verify_lock_loss


@pytest.mark.parametrize('round_number', range(1, 11))
async def test_ten_real_database_disconnects(round_number, owner_options, postgres_store):  # noqa: F811
    await verify_lock_loss(owner_options, postgres_store)
