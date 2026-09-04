"""Plane client initialization for MCP server."""

import os
from typing import NamedTuple

from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.dependencies import get_access_token
from fastmcp.utilities.logging import get_logger
from plane import PlaneClient

logger = get_logger(__name__)

# A self-hosted Plane instance can hold several workspaces, and one API key can
# reach more than one of them. PLANE_WORKSPACE_SLUG names the default; when
# PLANE_WORKSPACE_SLUGS lists more, a project id is resolved to the workspace
# that actually owns it. Resolution is opt-in: with the plural unset there is
# exactly one candidate and behaviour is unchanged.
WORKSPACES_ENV = "PLANE_WORKSPACE_SLUGS"

# project_id -> the workspace slug that answered for it. Populated on first use
# and never invalidated: a project does not move between workspaces.
_project_workspace: dict[str, str] = {}

# Project identifier (the JIMB in JIMB-274) -> workspace slug. A second map
# because the identifier-addressed calls never see a project id.
_identifier_workspace: dict[str, str] = {}


class PlaneClientContext(NamedTuple):
    """Context containing Plane client and workspace information."""

    client: PlaneClient
    workspace_slug: str


def candidate_workspaces(default_slug: str) -> list[str]:
    """The workspaces to try for a project, default first, in order, deduplicated."""
    ordered = [default_slug, *os.getenv(WORKSPACES_ENV, "").split(",")]
    seen: dict[str, None] = {}
    for slug in ordered:
        slug = slug.strip()
        if slug:
            seen.setdefault(slug, None)
    return list(seen)


def resolve_workspace(client: PlaneClient, project_id: str, default_slug: str) -> str:
    """Return the workspace slug that owns *project_id*.

    Asks each candidate in turn and keeps the one that answers. A project the
    workspace does not hold answers 404, and one outside the key's reach answers
    403 -- both are "not this one, try the next". When nothing answers, the
    default is returned unchanged so the caller raises the error it would have
    raised anyway; this never converts a real failure into a different one.
    """
    if not project_id:
        return default_slug

    cached = _project_workspace.get(project_id)
    if cached:
        return cached

    candidates = candidate_workspaces(default_slug)
    if len(candidates) < 2:
        # Nothing to choose between. Skip the probe entirely.
        return default_slug

    for slug in candidates:
        try:
            client.projects.retrieve(workspace_slug=slug, project_id=project_id)
        except Exception:  # noqa: BLE001 -- any refusal means "not this workspace"
            continue
        if slug != default_slug:
            logger.info("project %s resolved to workspace %s (default %s)", project_id, slug, default_slug)
        _project_workspace[project_id] = slug
        return slug

    logger.warning("project %s answered in none of %s; using %s", project_id, ",".join(candidates), default_slug)
    return default_slug


def resolve_workspace_for_identifier(client: PlaneClient, project_identifier: str, default_slug: str) -> str:
    """Return the workspace slug holding the project with this identifier.

    ``workitem retrieve_by_identifier`` is addressed as PROJECT-N and never
    carries a project id, so ``resolve_workspace`` has nothing to work with --
    yet PROJECT is exactly the thing that names the tenant. Each candidate's
    project list is read once and every id/identifier in it cached, so a board
    reached by key costs the same one call as a board reached by id.
    """
    if not project_identifier:
        return default_slug

    cached = _identifier_workspace.get(project_identifier)
    if cached:
        return cached

    candidates = candidate_workspaces(default_slug)
    if len(candidates) < 2:
        return default_slug

    for slug in candidates:
        try:
            response = client.projects.list_lite(workspace_slug=slug)
        except Exception:  # noqa: BLE001 -- a workspace the key cannot read is not the one
            continue
        found = False
        for project in getattr(response, "results", None) or []:
            identifier = getattr(project, "identifier", "") or ""
            project_id = str(getattr(project, "id", "") or "")
            if identifier:
                _identifier_workspace.setdefault(identifier, slug)
            if project_id:
                _project_workspace.setdefault(project_id, slug)
            if identifier == project_identifier:
                found = True
        if found:
            if slug != default_slug:
                logger.info(
                    "identifier %s resolved to workspace %s (default %s)", project_identifier, slug, default_slug
                )
            return slug

    logger.warning(
        "identifier %s appears in none of %s; using %s",
        project_identifier,
        ",".join(candidates),
        default_slug,
    )
    return default_slug


def get_plane_client_context(project_id: str = "") -> PlaneClientContext:
    """
    Initialize and return a PlaneClient instance with workspace context.

    Pass *project_id* from a project-scoped tool and the workspace is resolved to
    the one that owns that project -- see ``resolve_workspace``. Omit it for
    workspace-scoped tools, which stay on PLANE_WORKSPACE_SLUG.

    Authentication is handled by the PlaneOAuthProvider, which supports:
    1. Environment variables (PLANE_API_KEY + PLANE_WORKSPACE_SLUG)
    2. HTTP headers (x-api-key + x-workspace-slug)
    3. OAuth access token

    Environment variables:
    - PLANE_INTERNAL_BASE_URL: Internal URL for Plane API (preferred for server-to-server calls)
    - PLANE_BASE_URL: Base URL for Plane API (fallback, default: https://api.plane.so)

    Returns:
        PlaneClientContext containing configured PlaneClient instance and workspace slug

    Raises:
        ConfigurationError: If access token is not available or workspace slug is missing
    """
    base_url = os.getenv("PLANE_INTERNAL_BASE_URL") or os.getenv("PLANE_BASE_URL", "https://api.plane.so")
    workspace_slug = os.getenv("PLANE_WORKSPACE_SLUG", "")

    api_key = os.getenv("PLANE_API_KEY", "")
    access_token = None

    # Get access token from the OAuth provider (which handles all auth methods)
    stored_access_token: AccessToken | None = get_access_token()
    if stored_access_token:
        # Determine authentication method to use appropriate PlaneClient constructor
        auth_method = stored_access_token.claims.get("auth_method", "oauth")
        token = stored_access_token.token
        workspace_slug = stored_access_token.claims.get("workspace_slug", "")

        # For API key auth methods, use api_key parameter; for OAuth, use access_token
        if auth_method in ("api_key_env", "api_key_header"):
            api_key = token
        else:
            access_token = token

    if access_token:
        client = PlaneClient(
            base_url=base_url,
            access_token=access_token,
        )
    else:
        client = PlaneClient(
            base_url=base_url,
            api_key=api_key,
        )

    return PlaneClientContext(
        client=client,
        workspace_slug=resolve_workspace(client, project_id, workspace_slug),
    )
