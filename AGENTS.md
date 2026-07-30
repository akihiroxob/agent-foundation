# Repository Guidelines

## Project Structure & Module Organization

- `policy/runtime-policy.json` is the source of truth for command, filesystem, and network policy.
- `adapters/runtime_adapters.py` renders that policy into Codex and Claude Code formats.
- `scripts/generate_runtime_config.py` writes rendered files to `dist/` or checks them for drift.
- `tests/` contains adapter tests. Keep tests alongside the behavior they validate.
- `.apm/` holds the package's reusable instructions, skills, and hooks; `.github/workflows/verify.yml` defines CI.

Treat `dist/` as generated output. Do not hand-edit generated runtime configuration; update the policy or adapter, then regenerate it.

## Build, Test, and Development Commands

This repository uses Python 3.12 in CI and currently has no third-party Python dependencies.

```bash
python3 scripts/generate_runtime_config.py          # regenerate dist/
python3 scripts/generate_runtime_config.py --check  # fail if dist/ is stale
python3 -m unittest discover -s tests -v            # run all tests
apm compile --validate                              # validate the APM package, if APM is installed
```

Before submitting changes to policy rendering, run generation, the `--check` validation, and the test suite.

## Coding Style & Naming Conventions

Use Python's existing style: four-space indentation, `snake_case` for functions and variables, `PascalCase` for test classes, and type annotations for public helpers. Prefer standard-library modules and small, explicit functions. Keep JSON formatted with two-space indentation when it is emitted by code. Use descriptive test methods such as `test_codex_contains_expected_decisions`.

## Testing Guidelines

Tests use the standard-library `unittest` framework. Add or update focused tests in `tests/test_runtime_adapters.py` whenever rendering behavior changes. Assert both structural validity (for example, parsing generated JSON) and important emitted values. CI regenerates `dist/`, verifies it is current, and runs the full suite.

## Commit & Pull Request Guidelines

The existing history uses short, imperative subjects (for example, `initial commit`); follow that convention and keep commits focused. Pull requests should explain the policy or adapter change, list validation commands run, and include regenerated `dist/` files when output changes. Link relevant issues where available. Call out permission or security changes explicitly, since they affect generated runtime behavior.

## Security & Configuration

Never commit credentials, `.env` files, private keys, or local machine configuration. Keep destructive commands and sensitive-file access denied in `policy/runtime-policy.json`; review generated Codex and Claude settings whenever that policy changes.
