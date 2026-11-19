# Contributing to soapalin

## Requirements

- Python 3.8+
- Git
- A local virtual environment

## Setup

```bash
python -m venv .venv
. .venv/bin/activate        # Linux/macOS
.venv\Scripts\Activate.ps1  # Windows PowerShell
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

## Required checks

Before opening a pull request:

```bash
pytest
ruff check soapalin tests
mypy soapalin
```

If the CLI changes:

```bash
python soapalin_cli.py -h
python soapalin_cli.py migrate -h
python soapalin_cli.py provision -h
```

## Development rules

- The Zimbra source is always read-only.
- The destination is additive only; never add implicit cleanup or deletion.
- Tests must never depend on a real Zimbra server.
- Add or update tests for every business-logic change.
- Update `README.md` and CLI help when the interface changes.

## Pull requests

- Keep each pull request focused on one topic.
- Describe the problem, the solution, and the checks performed.
- Mention limitations and behavior changes.
- Do not include local data files (`*.csv`, snapshots, reports, logs).
