from datetime import datetime, timedelta, timezone
import json

import asyncpg
import pytest

from robot_agent.application.resources import Resources
from robot_agent.domain.models import TrustedContext


pytestmark = pytest.mark.integration
ADMIN = TrustedContext("test-customer", "user", "test-admin", frozenset({"maintainer"}))


async def test_resource_versions_immutable(postgres_store):
    resources = Resources(postgres_store, freshness_seconds=60, max_entries=100)
    first = await resources.put(ADMIN, "test-binding", "environment", {"table": "table description"}, ("test-survey:1",))
    second = await resources.put(ADMIN, "test-binding", "environment", {"table": "changed description"}, ("test-survey:2",))
    assert first["revision"] == 1
    assert second["revision"] == 2
    rows = await postgres_store.pool.fetch("SELECT revision,payload FROM ra_resource_versions ORDER BY revision")
    assert json.loads(rows[0]["payload"]) == {"table": "table description"}
    with pytest.raises(asyncpg.CheckViolationError):
        await postgres_store.pool.execute("UPDATE ra_resource_versions SET payload='{}'::jsonb")


async def test_resource_write_requires_maintainer(postgres_store):
    resources = Resources(postgres_store, freshness_seconds=60, max_entries=100)
    with pytest.raises(PermissionError):
        await resources.put(TrustedContext("test-customer", "user", "test-user"), "test-binding", "environment",
                            {"table": "test"}, ("test-survey",))
    assert await postgres_store.pool.fetchval("SELECT count(*) FROM ra_resource_versions") == 0


async def test_readiness_not_refreshed_by_unrelated_telemetry(postgres_store):
    resources = Resources(postgres_store, freshness_seconds=60, max_entries=100)
    await resources.put(ADMIN, "test-binding", "environment", {"table": "test table"}, ("test-survey",))
    await resources.put(ADMIN, "test-binding", "capability", {"pick": "test pick skill"}, ("test-preset",))
    stale = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()
    observations = {"connected": {"value": True, "received_at": fresh, "connection_generation": 1},
                    "skills": {"value": ["move_to", "vla_skill"], "received_at": fresh, "connection_generation": 1},
                    "waypoint_names": {"value": ["table"], "received_at": fresh, "connection_generation": 1},
                    "brain_skills": {"value": [{"skill_id": "pick"}], "received_at": fresh, "connection_generation": 1},
                    "nav_server_ready": {"value": True, "received_at": stale, "connection_generation": 1},
                    "battery": {"value": 80, "received_at": fresh, "connection_generation": 1}}
    await postgres_store.pool.execute("UPDATE ra_robot_bindings SET observations=$1::jsonb", json.dumps(observations))
    snap = await resources.snapshot(ADMIN)
    assert snap.waypoints["table"].endswith(":1:table")
    assert "nav_server_ready" not in snap.readiness


async def test_resource_does_not_invent_robot_skill(postgres_store):
    resources = Resources(postgres_store, freshness_seconds=60, max_entries=100)
    await resources.put(ADMIN, "test-binding", "environment", {"invented-table": "test"}, ("test-survey",))
    snap = await resources.snapshot(ADMIN)
    assert not snap.skills
    assert not snap.waypoints


async def test_reported_places_follow_current_map_and_connection(postgres_store, monkeypatch):
    # The clock can give two separate reports the same timestamp.
    clock = datetime.now(timezone.utc)

    class SameTick(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock

    monkeypatch.setattr("robot_agent.adapters.postgres.datetime", SameTick)
    resources = Resources(postgres_store, freshness_seconds=60, max_entries=100, use_reported_waypoints=True)
    binding = {"customer_id": ADMIN.customer_id, "binding_id": "test-binding"}

    async def report(**fields):
        async with postgres_store.system_unit_of_work() as tx:
            await tx.update_observations(binding, 1, fields)
        return await resources.snapshot(ADMIN)

    assert not (await report(connected=True, waypoint_names=["门口"])).waypoints
    assert not (await report(map="一楼")).waypoints  # Earlier names had no known map.
    first = await report(waypoint_names=["门口"])
    assert set(first.waypoints) == {"门口"}
    assert (await report(map="一楼")).waypoints == first.waypoints
    assert not (await report(map="二楼")).waypoints
    assert not (await report(map="一楼")).waypoints  # Switching back still needs a report.
    second = await report(map="二楼", waypoint_names=["门口"])
    assert set(second.waypoints) == {"门口"}
    assert second.waypoints["门口"] != first.waypoints["门口"]
    assert not (await report(waypoint_names="门口")).waypoints
    assert not (await report(waypoint_names=[None, "", "  "])).waypoints
    assert not (await report(connected=False, waypoint_names=["门口"])).waypoints

    stale = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    await postgres_store.pool.execute("""UPDATE ra_robot_bindings SET observations =
        jsonb_set(observations, '{map,received_at}', to_jsonb($1::text))""", stale)
    assert not (await report(connected=True, waypoint_names=["门口"])).waypoints
    await report(map="二楼", waypoint_names=["门口"])
    await postgres_store.pool.execute("UPDATE ra_robot_bindings SET connection_generation=connection_generation+1")
    assert not (await resources.snapshot(ADMIN)).waypoints
