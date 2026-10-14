"""Guards against garminconnect API drift (method names change between versions).

Skipped when the optional ``garmin`` extra is not installed (e.g. Python 3.11).
"""

from __future__ import annotations

import inspect

import pytest

garminconnect = pytest.importorskip("garminconnect")


def test_methods_used_by_the_provider_exist() -> None:
    cls = garminconnect.Garmin
    for name in (
        "login",
        "get_activities_by_date",
        "get_stats",
        "get_sleep_data",
        "get_hrv_data",
        "get_body_composition",
        "get_full_name",
    ):
        assert callable(getattr(cls, name, None)), f"garminconnect.Garmin.{name} is missing"
    params = inspect.signature(cls.__init__).parameters
    for name in ("email", "password", "prompt_mfa"):
        assert name in params, f"Garmin.__init__ no longer accepts {name}"
