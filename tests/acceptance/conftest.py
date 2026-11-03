import pytest


@pytest.fixture
def configuration():
    return {
        "database": {"dsn_env": "TEST_DSN"},
        "deployment": {"id": "test-deployment", "execution_enabled": False},
        "users": [{"customer_id": "test-customer", "actor_id": "test-user", "roles": ["user"], "token_env": "TEST_USER_TOKEN"}],
        "robots": [{"customer_id": "test-customer", "binding_id": "test-binding", "wire_robot_id": "test-robot",
                    "token_env": "TEST_ROBOT_TOKEN", "allowed_actions": ["move_to", "vla_skill"]}],
        "policy": {"version": "test-policy:v1", "approval_ttl_seconds": 300,
                   "actions": {"test-customer": {"move_to": "allow", "vla_skill": "require_confirmation"}}},
        "limits": {"freshness_seconds": 60, "max_resource_entries": 100, "max_audio_bytes": 1048576,
                   "max_frame_bytes": 8192, "max_text_chars": 2000, "max_plan_steps": 20,
                   "provider_response_bytes": 65536, "scan_interval": 0.1, "command_timeout": 30},
    }
