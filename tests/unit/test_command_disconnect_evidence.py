"""Development-only evidence examples, not database or end-to-end acceptance.

The initial ended task below is explicit input data for the pure reducer, not
a claim that a platform instance disconnected/committed/ended a real task.
"""
from dataclasses import replace
import unittest

from robot_agent.domain.models import Command, RobotEvent, Task
from robot_agent.domain.transitions import check_transition, is_abandoned
from robot_agent.domain.errors import make_error


class DisconnectEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.command = Command('cmd-A', 'turn-A', 'customer-A', 'unit-A', 'plan-A',
                               'step-A', 'robot-A', 'test_skill', {}, 1,
                               dispatch_state='SENT', outcome='RESULT_UNKNOWN')
        self.task = Task('turn-A', 'customer-A', 'user', 'user-A', 'development example',
                         state='CANCELLED', reason_code='COMMAND_CONNECTION_LOST',
                         commands=(self.command,), advancement_stopped=True,
                         cancel_requested=True)

    def event(self, state, **updates):
        return replace(RobotEvent('event-A', 'customer-A', 'robot-A', 2, 'cmd-A', state), **updates)

    def test_connection_loss_is_policy_abandonment(self):
        self.assertTrue(is_abandoned(self.task))
        self.assertFalse(is_abandoned(replace(self.task, state='RUNNING')))

    def test_late_terminal_refines_evidence_without_resuming_task(self):
        for state, outcome in (('succeeded', 'SUCCEEDED'), ('failed', 'FAILED')):
            with self.subTest(state=state):
                changed = check_transition(self.task, self.command, self.event(state), current_generation=2)
                self.assertEqual(changed.command.outcome, outcome)
                self.assertEqual(changed.task.state, 'CANCELLED')
                self.assertEqual(changed.task.reason_code, 'COMMAND_CONNECTION_LOST')
                self.assertEqual(changed.task.completed, ())
                self.assertTrue(changed.task.advancement_stopped)

    def test_old_connection_receipt_cannot_change_evidence(self):
        changed = check_transition(self.task, self.command,
                                   self.event('succeeded', connection_generation=1), current_generation=2)
        self.assertEqual(changed.task, self.task)

    def test_late_running_does_not_clear_unknown_result(self):
        changed = check_transition(self.task, self.command, self.event('running'), current_generation=2)
        self.assertEqual(changed.task, self.task)

    def test_conflicting_late_result_marks_evidence_without_reopening(self):
        command = replace(self.command, outcome='SUCCEEDED')
        task = replace(self.task, commands=(command,))
        changed = check_transition(task, command, self.event('failed'), current_generation=2)
        self.assertTrue(changed.command.evidence_conflict)
        self.assertTrue(changed.task.result_disputed)
        self.assertEqual(changed.task.state, 'CANCELLED')
        self.assertEqual(changed.task.reason_code, 'COMMAND_CONNECTION_LOST')

    def test_new_reason_has_public_transport_error(self):
        error = make_error('COMMAND_CONNECTION_LOST')
        self.assertEqual(error['code'], 'COMMAND_CONNECTION_LOST')
        self.assertEqual(error['source'], 'transport')


if __name__ == '__main__':
    unittest.main()
