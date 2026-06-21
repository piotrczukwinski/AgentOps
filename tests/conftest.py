"""Shared test fixtures and helpers for the AgentOps test suite.

The shared helpers below were originally defined in
``tests.test_gated_roadmap`` and imported by several other test files.
They remain defined there to avoid breaking those existing imports; this
module re-exports them so ``tests.conftest`` is the canonical import path
going forward.

It also provides pytest fixtures (``init_repo``, ``make_fake_codex``) as
the forward-looking path so new tests can depend on fixtures rather than
importing private helpers.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from tests.test_gated_roadmap import (
    FakeCodexService,
    ScriptedVerdict,
    UnavailableCodexService,
    _init_repo,
    git,
)

__all__ = [
    "FakeCodexService",
    "ScriptedVerdict",
    "UnavailableCodexService",
    "_init_repo",
    "git",
    "init_repo",
    "make_fake_codex",
]


@pytest.fixture
def init_repo(tmp_path: Path) -> Path:
    """Yield a fresh git repo created under ``tmp_path``.

    The repo lives inside the pytest-managed ``tmp_path``, so cleanup is
    handled automatically when the fixture tears down.
    """
    repo = _init_repo(tmp_path)
    yield repo


@pytest.fixture
def make_fake_codex() -> Callable[[list[ScriptedVerdict]], FakeCodexService]:
    """Return a factory that builds a :class:`FakeCodexService`.

    Example::

        def test_something(make_fake_codex):
            codex = make_fake_codex([ScriptedVerdict(verdict="ACCEPT")])
    """

    def _factory(verdicts: list[ScriptedVerdict]) -> FakeCodexService:
        return FakeCodexService(list(verdicts))

    return _factory