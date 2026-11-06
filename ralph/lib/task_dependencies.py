"""Explicit Project scheduling prerequisites; no inference from prose or sort order."""
from __future__ import annotations


def task_dependencies(config: dict) -> dict[str, list[str]]:
    value = config.get('taskDependencies', {})
    if not isinstance(value, dict):
        raise ValueError('taskDependencies must map Task IDs to prerequisite ID arrays')
    for task_id, dependencies in value.items():
        if (not isinstance(task_id, str) or not task_id.strip() or not isinstance(dependencies, list)
                or any(not isinstance(item, str) or not item.strip() for item in dependencies)
                or len(dependencies) != len(set(dependencies))):
            raise ValueError('taskDependencies must map non-empty Task IDs to unique prerequisite ID arrays')
    visiting, visited = set(), set()
    def visit(task_id):
        if task_id in visiting:
            raise ValueError('taskDependencies contains a cycle at Task ' + task_id)
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in value.get(task_id, []):
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)
    for task_id in value:
        visit(task_id)
    return value


def unmet_dependencies(config: dict, task_id: str, tasks: list[dict]) -> list[str]:
    accepted = {task['id'] for task in tasks if task.get('status') == 'accepted'}
    return [dependency for dependency in task_dependencies(config).get(task_id, []) if dependency not in accepted]


def accepted_prerequisites(workflow, project_id: str) -> list[dict]:
    if not task_dependencies(workflow.config):
        return []
    # Missing records (including a response limit) fail closed, never imply completion.
    return workflow.mcp('list_tasks', {'projectId': project_id, 'filter': {'status': ['accepted']}, 'limit': 500})['tasks']


def filter_dependencies(config: dict, role: str, tasks: list[dict], accepted: list[dict]):
    if role != 'worker':
        return tasks, []
    ready, waiting = [], []
    for task in tasks:
        missing = unmet_dependencies(config, task['id'], accepted)
        if missing:
            waiting.append({'taskId': task['id'], 'prerequisiteIds': missing})
        else:
            ready.append(task)
    return ready, waiting
