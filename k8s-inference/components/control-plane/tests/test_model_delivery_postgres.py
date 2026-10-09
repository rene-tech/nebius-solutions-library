"""The one-attempt contract is enforced by durable production store state too."""

# ruff: noqa: F811 -- imported fixture is injected by pytest
import pytest
from test_model_delivery_contracts import exercise_delivery
from test_postgres_integration import postgres_store  # noqa: F401


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["http503", "invalid_media", "timeout", "success", "lease_expiry"])
async def test_postgres_transfer_terminal_delivery_and_replay(registry, postgres_store, failure):
    await exercise_delivery(registry, postgres_store, failure)
