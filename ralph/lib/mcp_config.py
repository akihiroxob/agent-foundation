"""Read project MCP definitions and adapt them without server-specific branches."""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path


def read_servers(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"Cannot read MCP configuration: {path}") from error
    if not isinstance(value, dict) or not isinstance(value.get("mcpServers"), dict):
        raise ValueError(f"mcpServers must be an object: {path}")
    servers = value["mcpServers"]
    for name, server in servers.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError("MCP server names must contain only letters, digits, underscores or hyphens")
        if not isinstance(server, dict):
            raise ValueError(f"MCP server {name} must be an object")
        transport = server.get("type", "stdio")
        if transport not in {"stdio", "http", "sse"}:
            raise ValueError(f"Unsupported MCP transport for {name}")
        required = "command" if transport == "stdio" else "url"
        if not isinstance(server.get(required), str) or not server[required] or "\0" in server[required]:
            raise ValueError(f"MCP server {name} requires {required}")
        args = server.get("args", [])
        if not isinstance(args, list) or any(not isinstance(arg, str) or "\0" in arg for arg in args):
            raise ValueError(f"MCP server {name} args must be a string array")
        for field in ("env", "headers"):
            entries = server.get(field, {})
            if not isinstance(entries, dict) or any(not isinstance(k, str) or not isinstance(v, str) or
                    "\0" in k or "\0" in v for k, v in entries.items()):
                raise ValueError(f"MCP server {name} {field} must contain strings")
        if "cwd" in server and (not isinstance(server["cwd"], str) or "\0" in server["cwd"]):
            raise ValueError(f"MCP server {name} cwd must be a string")
        if "timeout" in server and (isinstance(server["timeout"], bool) or
                not isinstance(server["timeout"], (int, float)) or not math.isfinite(server["timeout"]) or server["timeout"] <= 0):
            raise ValueError(f"MCP server {name} timeout must be positive milliseconds")
    return servers


def toml_value(value) -> str:
    if isinstance(value, dict):
        return "{" + ", ".join(json.dumps(k) + "=" + toml_value(v) for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(toml_value(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def codex_arguments(servers: dict) -> list[str]:
    arguments = []
    for name, server in servers.items():
        # Wacha authentication remains owned by the Codex Provider.
        if name == "wacha":
            continue
        transport = server.get("type", "stdio")
        if transport == "sse":
            raise ValueError(f"Codex does not support the legacy SSE transport for MCP server {name}; use http")
        values = {"required": True, "default_tools_approval_mode": "approve",
                  "startup_timeout_sec": 120, "tool_timeout_sec": server.get("timeout", 3660000) / 1000}
        if transport == "stdio":
            values.update(command=server["command"], args=server.get("args", []))
            if "cwd" in server:
                values["cwd"] = server["cwd"]
            for key, value in server.get("env", {}).items():
                # Forward whole-variable references by name, keeping credentials out of CLI arguments.
                variable = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value)
                if variable and variable[1] == key:
                    values.setdefault("env_vars", []).append(key)
                elif "${" in value:
                    raise ValueError(f"Codex MCP server {name} env.{key} must use a same-name whole-variable reference")
                else:
                    values.setdefault("env", {})[key] = value
        else:
            values["url"] = server["url"]
            if "${" in server["url"]:
                raise ValueError(f"Codex MCP server {name} url cannot contain an environment reference")
            for key, value in server.get("headers", {}).items():
                variable = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value)
                bearer = re.fullmatch(r"Bearer \$\{([A-Za-z_][A-Za-z0-9_]*)\}", value)
                if key.lower() == "authorization" and bearer:
                    values["bearer_token_env_var"] = bearer[1]
                elif variable:
                    values.setdefault("env_http_headers", {})[key] = variable[1]
                elif "${" in value:
                    raise ValueError(f"Unsupported environment reference in MCP server {name} header {key}")
                else:
                    values.setdefault("http_headers", {})[key] = value
        for key, value in values.items():
            arguments.extend(["-c", f"mcp_servers.{name}.{key}={toml_value(value)}"])
    return arguments


def provider_arguments(provider: str, path: Path) -> list[str]:
    servers = read_servers(path)
    if provider == "codex":
        return codex_arguments(servers)
    if not path.exists():
        return []
    arguments = ["--mcp-config", str(path), "--strict-mcp-config"]
    if servers:
        arguments += ["--allowedTools", *[f"mcp__{name}" for name in servers]]
    return arguments


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=["codex", "claude"], required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        arguments = provider_arguments(args.provider, args.config)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    for argument in arguments:
        sys.stdout.buffer.write(argument.encode("utf-8") + b"\0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
