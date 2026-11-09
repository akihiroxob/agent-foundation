from __future__ import annotations
import json
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from ralph.lib.operation_recovery import OperationRecovery, RecoveryDeferred

class RecoveryBudgetTest(unittest.TestCase):
    def recovery(self, state):
        value = OperationRecovery()
        value.state = state
        value.config = {}
        value.save = lambda: None
        return value

    def test_wall_clock_sleep_does_not_exhaust_new_repair_budget(self):
        value = self.recovery({'recoveryBudget': {'startedAt': 1000, 'startedMonotonic': 10}})
        with patch('ralph.lib.operation_recovery.time.time', return_value=100000), patch('ralph.lib.operation_recovery.time.monotonic', return_value=25):
            self.assertEqual(value.recovery_remaining(), 885)
        with patch('ralph.lib.operation_recovery.time.monotonic', return_value=920):
            self.assertEqual(value.recovery_remaining(), 0)

    def test_legacy_verification_deadline_is_archived_without_removing_attempt_bound(self):
        old = {'startedAt': 1, 'attempts': 1}
        value = self.recovery({'recoveryBudget': old, 'verificationRetry': {'attempt': 2}})
        value.separate_verification_budget()
        self.assertNotIn('recoveryBudget', value.state)
        self.assertEqual(value.state['verificationRetry']['previousRecoveryBudget'], old)
        self.assertEqual(value.reserve_verification_retry(), (3, 3))
        value.state['verificationRetry']['attempt'] = 3
        value.defer_execution = lambda reason: None
        with self.assertRaises(RecoveryDeferred):
            value.reserve_verification_retry()

    def test_operation_repair_deadline_is_preserved(self):
        value = self.recovery({'recoveryBudget': {'startedAt': 1}, 'verificationRetry': {'attempt': 1}, 'operationRecovery': {'stage': 'finish'}})
        value.separate_verification_budget()
        self.assertIn('recoveryBudget', value.state)

class RunnerSleepTest(unittest.TestCase):
    def test_inhibits_only_idle_sleep_on_macos_and_keeps_other_platforms_unchanged(self):
        functions = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'ralph/bin/ralph'))
        command = functions['runner_command']
        with patch('sys.platform', 'darwin'), patch('shutil.which', return_value='/usr/bin/caffeinate'):
            wrapped = command(Path('/tmp/config.json'), ['worker'])
            self.assertEqual(wrapped[:2], ['/usr/bin/caffeinate', '-i'])
            self.assertEqual(wrapped[-1], 'worker')
        with patch('sys.platform', 'linux'), patch('shutil.which') as lookup:
            plain = command(Path('/tmp/config.json'), ['worker'])
            lookup.assert_not_called()
        self.assertEqual(wrapped[2:], plain)
