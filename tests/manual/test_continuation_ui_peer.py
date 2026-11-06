"""Offline peer guards; no business fixtures, network, database, or model."""
import asyncio
import unittest

if __package__:
    from .continuation_ui_peer import (
        OUTCOMES, ReadOnlySuite, continuation_base, permitted_command, task_blocks_start,
    )
    from .real_no_retry_acceptance import WAYPOINTS
else:
    from continuation_ui_peer import (
        OUTCOMES, ReadOnlySuite, continuation_base, permitted_command, task_blocks_start,
    )
    from real_no_retry_acceptance import WAYPOINTS


class ContinuationPeerGuards(unittest.TestCase):
    def test_only_main_isolated_loopback_is_accepted(self):
        self.assertEqual(continuation_base("http://127.0.0.1:18873/"), "http://127.0.0.1:18873")
        for value in ("http://127.0.0.1:18874", "http://127.0.0.1:18763",
                      "http://192.168.201.39:18873", "http://192.168.201.9:18873",
                      "http://localhost:18873", "http://127.0.0.1:18873/tasks"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                continuation_base(value)

    def test_stopped_tasks_with_unknown_allocations_still_block_start(self):
        self.assertTrue(task_blocks_start({"state": "BLOCKED", "advancement_stopped": True,
                                           "allocation_robot_binding_id": "test-no-retry-binding"}))
        self.assertTrue(task_blocks_start({"state": "WAITING_APPROVAL", "advancement_stopped": False}))
        self.assertTrue(task_blocks_start({"state": "RECEIVED"}))
        self.assertFalse(task_blocks_start({"state": "SUCCEEDED"}))
        self.assertFalse(task_blocks_start({"state": "BLOCKED", "advancement_stopped": True}))

    def test_only_named_simulator_actions_allowed(self):
        self.assertTrue(permitted_command({"cmd_id": "test", "skill": "move_to",
                                           "args": {"location": WAYPOINTS[0]}}))
        self.assertFalse(permitted_command({"cmd_id": "test", "skill": "move_to",
                                            "args": {"location": "actual-office"}}))
        self.assertFalse(permitted_command({"cmd_id": "test", "skill": "unknown", "args": {}}))
        self.assertFalse(permitted_command({"skill": "move_to", "args": {"location": WAYPOINTS[0]}}))
        self.assertEqual(OUTCOMES, ("succeeded", "succeeded", "failed", "succeeded"))

    def test_http_writes_are_rejected_before_client_access(self):
        suite = ReadOnlySuite.__new__(ReadOnlySuite)
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method), self.assertRaises(ValueError):
                asyncio.run(suite.request(method, "/api/robot-agent/tasks"))


if __name__ == "__main__":
    unittest.main()
