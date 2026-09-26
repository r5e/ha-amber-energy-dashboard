"""Shared test fixtures."""

import json
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures"

# Request fixtures explicitly rather than via an autouse fixture: the recorder fixture
# must be set up before `hass`, and an autouse fixture that depends on `hass` breaks that.


def load_fixture(name: str) -> str:
    """Return a fixture file as text."""
    return (FIXTURES / name).read_text()


def load_json_fixture(name: str) -> Any:
    """Return a fixture file decoded from JSON."""
    return json.loads(load_fixture(name))
