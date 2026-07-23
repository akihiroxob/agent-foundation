from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _quoted(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_codex(policy: dict[str, Any]) -> dict[str, str]:
    override = policy.get("overrides", {}).get("codex", {})
    config = (
        f'approval_policy = {_quoted(override.get("approval_policy", "on-request"))}\n'
        f'sandbox_mode = {_quoted(override.get("sandbox_mode", "workspace-write"))}\n'
    )

    decision_map = {"allow": "allow", "ask": "prompt", "deny": "forbidden"}
    rules: list[str] = ["# Generated from policy/runtime-policy.json. Do not edit directly.", ""]
    for category in ("allow", "ask", "deny"):
        for command in policy["commands"].get(category, []):
            pattern = ", ".join(_quoted(part) for part in command)
            rules.extend([
                "prefix_rule(",
                f"    pattern = [{pattern}],",
                f'    decision = "{decision_map[category]}",',
                ")",
                "",
            ])

    return {
        ".codex/config.toml": config,
        ".codex/rules/default.rules": "\n".join(rules),
    }


def _claude_bash_rule(command: list[str]) -> str:
    return f'Bash({" ".join(command)} *)'


def render_claude(policy: dict[str, Any]) -> dict[str, str]:
    permissions: dict[str, Any] = {
        "defaultMode": policy.get("overrides", {}).get("claude", {}).get("defaultMode", "acceptEdits"),
        "allow": [_claude_bash_rule(c) for c in policy["commands"].get("allow", [])],
        "ask": [_claude_bash_rule(c) for c in policy["commands"].get("ask", [])],
        "deny": [_claude_bash_rule(c) for c in policy["commands"].get("deny", [])],
    }
    permissions["deny"].extend(
        f"Read(./{path})" for path in policy.get("filesystem", {}).get("deny_read", [])
    )
    settings = {"permissions": permissions}
    return {".claude/settings.json": json.dumps(settings, ensure_ascii=False, indent=2) + "\n"}


def write_files(root: Path, files: dict[str, str]) -> None:
    for relative_path, content in files.items():
        target = root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
