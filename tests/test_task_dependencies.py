from __future__ import annotations
import unittest
from ralph.lib.task_dependencies import task_dependencies, filter_dependencies
from ralph.lib.execution_status import filter_summary

class TaskDependenciesTest(unittest.TestCase):
    def test_invalid_graphs_fail_closed(self):
        for graph in ([], {'a': 'b'}, {'a': ['a']}, {'a': ['b'], 'b': ['a']}, {'a': ['b', 'b']}, {'a': ['']}):
            with self.subTest(graph=graph), self.assertRaises(ValueError):
                task_dependencies({'taskDependencies': graph})

    def test_all_dependencies_must_be_accepted_and_unknown_or_canceled_is_not_success(self):
        config = {'taskDependencies': {'a': ['b', 'c']}}
        tasks = [{'id': 'a'}, {'id': 'independent'}]
        for status in ('todo', 'in_review', 'wait_accept', 'canceled', None):
            ready, waiting = filter_dependencies(config, 'worker', tasks,
                [{'id': 'b', 'status': 'accepted'}, {'id': 'c', 'status': status}])
            self.assertEqual(ready, [{'id': 'independent'}])
            self.assertEqual(waiting, [{'taskId': 'a', 'prerequisiteIds': ['c']}])
        self.assertEqual(filter_dependencies(config, 'worker', tasks,
            [{'id': 'b', 'status': 'accepted'}, {'id': 'c', 'status': 'accepted'}]), (tasks, []))
        self.assertEqual(filter_dependencies(config, 'reviewer', tasks, []), (tasks, []))
