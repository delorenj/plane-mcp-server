"""One API key, several workspaces on one self-hosted Plane.

A project-scoped tool must reach the workspace that actually owns the project.
Pinning the whole process to PLANE_WORKSPACE_SLUG made every write to a board in
a second workspace answer 403, which reads as a bad credential and is not one.
"""

from __future__ import annotations

import pytest

from plane_mcp import client as client_mod
from plane_mcp.client import (
    candidate_workspaces,
    resolve_workspace,
    resolve_workspace_for_identifier,
)


class _Projects:
    """Answers for the workspaces it holds; refuses the rest, as Plane does."""

    def __init__(self, holdings: dict[str, set[str]]) -> None:
        self.holdings = holdings
        self.asked: list[tuple[str, str]] = []

    def retrieve(self, workspace_slug: str, project_id: str):
        self.asked.append((workspace_slug, project_id))
        if project_id not in self.holdings.get(workspace_slug, set()):
            raise RuntimeError("403 Forbidden")
        return {"id": project_id}


class _Client:
    def __init__(self, holdings: dict[str, set[str]]) -> None:
        self.projects = _Projects(holdings)


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    monkeypatch.setattr(client_mod, "_project_workspace", {})
    monkeypatch.delenv("PLANE_WORKSPACE_SLUGS", raising=False)


def test_candidates_put_the_default_first_and_dedupe(monkeypatch):
    monkeypatch.setenv("PLANE_WORKSPACE_SLUGS", " automaticai , 33god ,, automaticai ")
    assert candidate_workspaces("33god") == ["33god", "automaticai"]


def test_one_candidate_never_probes():
    c = _Client({"33god": {"p1"}})
    assert resolve_workspace(c, "p1", "33god") == "33god"
    assert c.projects.asked == [], "a single candidate must cost no API call"


def test_a_project_in_the_second_workspace_resolves_there(monkeypatch):
    monkeypatch.setenv("PLANE_WORKSPACE_SLUGS", "automaticai")
    c = _Client({"33god": {"p1"}, "automaticai": {"jimb"}})
    assert resolve_workspace(c, "jimb", "33god") == "automaticai"
    assert c.projects.asked == [("33god", "jimb"), ("automaticai", "jimb")]


def test_the_answer_is_cached_per_project(monkeypatch):
    monkeypatch.setenv("PLANE_WORKSPACE_SLUGS", "automaticai")
    c = _Client({"33god": {"p1"}, "automaticai": {"jimb"}})
    assert resolve_workspace(c, "jimb", "33god") == "automaticai"
    before = len(c.projects.asked)
    assert resolve_workspace(c, "jimb", "33god") == "automaticai"
    assert len(c.projects.asked) == before, "a resolved project must not be probed again"


def test_no_project_id_keeps_the_default(monkeypatch):
    monkeypatch.setenv("PLANE_WORKSPACE_SLUGS", "automaticai")
    c = _Client({"automaticai": {"jimb"}})
    assert resolve_workspace(c, "", "33god") == "33god"
    assert c.projects.asked == []


def test_an_unknown_project_falls_back_rather_than_inventing(monkeypatch):
    """The caller must raise the error it would have raised anyway."""
    monkeypatch.setenv("PLANE_WORKSPACE_SLUGS", "automaticai")
    c = _Client({"33god": set(), "automaticai": set()})
    assert resolve_workspace(c, "ghost", "33god") == "33god"
    assert len(c.projects.asked) == 2
    assert "ghost" not in client_mod._project_workspace, "a failed probe must not poison the cache"


class _LiteProject:
    def __init__(self, pid: str, identifier: str) -> None:
        self.id = pid
        self.identifier = identifier


class _LiteResponse:
    def __init__(self, projects):
        self.results = projects


class _ProjectsWithLite(_Projects):
    """Adds the projects-lite listing the identifier path reads."""

    def __init__(self, holdings, catalog: dict[str, list[_LiteProject]]) -> None:
        super().__init__(holdings)
        self.catalog = catalog
        self.listed: list[str] = []

    def list_lite(self, workspace_slug: str):
        self.listed.append(workspace_slug)
        if workspace_slug not in self.catalog:
            raise RuntimeError("403 Forbidden")
        return _LiteResponse(self.catalog[workspace_slug])


@pytest.fixture(autouse=True)
def _clear_identifier_cache(monkeypatch):
    monkeypatch.setattr(client_mod, "_identifier_workspace", {})


def _identifier_client():
    c = _Client({})
    c.projects = _ProjectsWithLite(
        {},
        {
            "33god": [_LiteProject("p1", "BB"), _LiteProject("p2", "CS")],
            "automaticai": [_LiteProject("jimb", "JIMB")],
        },
    )
    return c


def test_a_ticket_key_resolves_its_tenant(monkeypatch):
    """JIMB-274 names the project but carries no project id."""
    monkeypatch.setenv("PLANE_WORKSPACE_SLUGS", "automaticai")
    c = _identifier_client()
    assert resolve_workspace_for_identifier(c, "JIMB", "33god") == "automaticai"
    assert c.projects.listed == ["33god", "automaticai"]


def test_listing_a_workspace_also_caches_its_project_ids(monkeypatch):
    """The identifier probe is the cheapest place to learn every id in reach."""
    monkeypatch.setenv("PLANE_WORKSPACE_SLUGS", "automaticai")
    c = _identifier_client()
    resolve_workspace_for_identifier(c, "JIMB", "33god")
    assert client_mod._project_workspace["p1"] == "33god"
    assert client_mod._project_workspace["jimb"] == "automaticai"
    assert resolve_workspace(c, "p1", "33god") == "33god"
    assert c.projects.asked == [], "the id map was already filled; no second probe"


def test_identifier_answers_are_cached(monkeypatch):
    monkeypatch.setenv("PLANE_WORKSPACE_SLUGS", "automaticai")
    c = _identifier_client()
    resolve_workspace_for_identifier(c, "JIMB", "33god")
    before = len(c.projects.listed)
    assert resolve_workspace_for_identifier(c, "JIMB", "33god") == "automaticai"
    assert len(c.projects.listed) == before


def test_one_candidate_never_lists(monkeypatch):
    c = _identifier_client()
    assert resolve_workspace_for_identifier(c, "JIMB", "33god") == "33god"
    assert c.projects.listed == []


def test_an_unknown_identifier_falls_back(monkeypatch):
    monkeypatch.setenv("PLANE_WORKSPACE_SLUGS", "automaticai")
    c = _identifier_client()
    assert resolve_workspace_for_identifier(c, "GHOST", "33god") == "33god"
