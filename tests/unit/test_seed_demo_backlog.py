"""Tests for scripts/seed_demo_backlog.py.

The safety guard is the point. This script writes fixture rows to whatever
DATABASE_URL names, and a developer's .env very often names the live database —
it did on the day this was written. A refusal that only worked by convention
would be worth nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.seed_demo_backlog import DEMO_SOURCE_NAME, build_parser, main


class TestProductionGuard:
    def test_refuses_to_run_in_production(self, capsys):
        with patch("config.settings.settings.__class__.is_production", property(lambda self: True)):
            assert main(["--count", "10"]) == 1
        assert "REFUSING" in capsys.readouterr().err

    def test_refuses_even_for_clean_in_production(self, capsys):
        # --clean deletes rows. If anything must not run against production,
        # it is the delete path.
        with patch("config.settings.settings.__class__.is_production", property(lambda self: True)):
            assert main(["--clean"]) == 1
        assert "REFUSING" in capsys.readouterr().err


class TestArguments:
    def test_count_defaults_to_100(self):
        assert build_parser().parse_args([]).count == 100

    def test_clean_is_off_by_default(self):
        assert build_parser().parse_args([]).clean is False

    def test_clean_flag_parses(self):
        assert build_parser().parse_args(["--clean"]).clean is True


class TestFixtureIdentity:
    def test_fixture_source_is_clearly_labelled(self):
        """The name is how --clean finds its own rows, and how a human reading
        the sources table knows these are not real articles."""
        assert "synthetic" in DEMO_SOURCE_NAME.lower()
