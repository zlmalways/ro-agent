"""Offline safety tests only: never connect to a server or create task business facts."""
import json

import pytest

from tests.manual.real_no_retry_acceptance import Evidence, IsolatedWebSocket, load_credentials, safe_base_url


@pytest.mark.parametrize("address", [
    "http://127.0.0.1:18763", "http://localhost:18873", "http://192.168.1.39:18873",
    "http://192.168.1.9:18873", "http://0.0.0.0:18873", "http://[::1]:18873",
    "http://127.0.0.1:18875", "https://127.0.0.1:18873", "http://127.0.0.1:18873/api",
    "http://127.0.0.1:18873?other=18763", "http://127.0.0.1:18873#other",
    "http://user@127.0.0.1:18873", "http://127.0.0.1.example.com:18873",
])
def test_reject_every_endpoint_outside_exact_isolated_loopback_ports(address):
    with pytest.raises(ValueError):
        safe_base_url(address)


@pytest.mark.parametrize("port", [18873, 18874])
def test_accept_only_isolated_loopback_ports(port):
    assert safe_base_url(f"http://127.0.0.1:{port}/") == f"http://127.0.0.1:{port}"


def test_websocket_handshake_never_follows_redirects():
    error = RuntimeError("redirect to forbidden production endpoint")
    assert IsolatedWebSocket.process_redirect(None, error) is error


def test_reject_non_test_robot_identity_before_network(tmp_path):
    file = tmp_path / "credentials.json"
    file.write_text(json.dumps({"web_token": "test-user-secret", "robot_token": "test-robot-secret",
                                "robot_id": "physical-robot", "binding_id": "test-no-retry-binding"}))
    with pytest.raises(ValueError, match="real robot identities are forbidden"):
        load_credentials(file)


def test_scrub_credentials_from_every_saved_evidence_value(tmp_path):
    evidence = Evidence(tmp_path, ["private-test-token"])
    evidence.record("error", message="request failed for private-test-token")
    evidence.save("summary.json", {"nested": ["private-test-token"]})
    for file in tmp_path.iterdir():
        assert "private-test-token" not in file.read_text(encoding="utf-8")
        assert "[REDACTED]" in file.read_text(encoding="utf-8")
