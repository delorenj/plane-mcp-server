"""The advertised per_page ceiling.

Plane refuses per_page above 100 and the SDK enforces it with a Pydantic
validator, so an over-large value is an exception rather than a smaller page.
The parameter used to be declared bare -- `per_page: int = 0` -- which told a
model nothing, and a grooming agent asked for 300. The three identical
validation errors that came back read to the calling harness as three
consecutive tool failures, tripping its circuit breaker and taking every Plane
tool offline for a minute.

So the bound is asserted in the schema the model actually reads, not just in
behaviour: a description that names the range, and machine-readable `maximum`.
Nothing else proves the ceiling is visible before the call is made.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from fastmcp import FastMCP

from plane_mcp.toolkit import PER_PAGE_MAX

os.environ.setdefault("PLANE_API_KEY", "test")
os.environ.setdefault("PLANE_WORKSPACE_SLUG", "test")


@pytest.fixture(scope="module")
def paged() -> dict[str, dict]:
    """Every tool that advertises per_page, mapped to that property's schema."""
    from plane_mcp.tools import register_tools

    loop = asyncio.new_event_loop()
    mcp = FastMCP("per-page-bound")
    register_tools(mcp)
    tools = loop.run_until_complete(mcp.list_tools())

    out = {}
    for tool in tools:
        prop = ((tool.parameters or {}).get("properties") or {}).get("per_page")
        if prop is not None:
            out[tool.name] = prop
    return out


def test_every_paged_tool_advertises_the_ceiling(paged):
    assert len(paged) >= 20, f"expected the paging tools, found {sorted(paged)}"
    missing = sorted(name for name, prop in paged.items() if prop.get("maximum") != PER_PAGE_MAX)
    assert not missing, f"per_page carries no maximum on: {missing}"


def test_the_ceiling_is_stated_in_prose(paged):
    """A `maximum` alone is a number in a schema; the model reads the description."""
    silent = sorted(name for name, prop in paged.items() if str(PER_PAGE_MAX) not in (prop.get("description") or ""))
    assert not silent, f"per_page description does not name the ceiling on: {silent}"


def test_the_description_points_at_the_alternative(paged):
    """Refusing a bigger page is only useful next to the way to get more rows."""
    quiet = sorted(name for name, prop in paged.items() if "cursor" not in (prop.get("description") or ""))
    assert not quiet, f"per_page description does not mention cursor on: {quiet}"


def test_zero_stays_allowed(paged):
    """0 means "let Plane choose" and is the default; a `minimum` of 1 would break it."""
    wrong = sorted(name for name, prop in paged.items() if prop.get("minimum") not in (0, None))
    assert not wrong, f"per_page refuses its own default on: {wrong}"
