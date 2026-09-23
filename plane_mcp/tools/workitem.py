"""Work items: the core issue/task/epic record.

`state__group` is a valid `group_by` value for the count action but is NOT a
filterable PQL field. The two vocabularies are documented separately below;
merging them teaches the model to filter on a field the API rejects.
"""

from __future__ import annotations

import os
from html import escape
from typing import Annotated, Any, Literal, get_args

from fastmcp import FastMCP
from fastmcp.utilities.logging import get_logger
from plane.errors.errors import HttpError
from plane.models.enums import PriorityEnum
from plane.models.query_params import (
    RetrieveQueryParams,
    WorkItemCountQueryParams,
    WorkItemQueryParams,
)
from plane.models.work_items import (
    CreateWorkItem,
    PaginatedWorkItemResponse,
    UpdateWorkItem,
    WorkItem,
    WorkItemDetail,
    WorkItemSearch,
)
from pydantic import Field

from plane_mcp.client import get_plane_client_context, resolve_workspace_for_identifier
from plane_mcp.pql_reference import PQL_FIELD_HINT
from plane_mcp.toolkit import (
    Action,
    PerPage,
    build_annotations,
    build_description,
    coerce_list,
    envelope,
    ids_of,
    missing,
    one_of,
    opt,
    pql_failure,
)

logger = get_logger(__name__)

NAME = "workitem"
TITLE = "Work items"

PRIORITIES = get_args(PriorityEnum)

WRITE_FIELDS = (
    "name",
    "assignees",
    "labels",
    "type_id",
    "point",
    "description_html",
    "description_stripped",
    "priority",
    "start_date",
    "target_date",
    "sort_order",
    "is_draft",
    "parent",
    "state",
    "estimate_point",
    "external_source",
    "external_id",
)
QUERY_FIELDS = ("order_by", "per_page", "cursor", "expand", "fields", "external_id", "external_source")

# Plane's work item PATCH replaces the whole labels (and assignees) list. A list
# the caller read earlier erases whatever another writer added since -- the
# Ticket Pickup Chip's `agent:working`, a `px claim` assignee -- so `update`
# refuses them and the manage_* actions re-read the item and apply a delta
# immediately before the write.
SET_FIELDS = ("labels", "assignees")

# Labels that belong to the pipeline, not to whoever calls this tool.
# `agent:working` is put on by the Ticket Pickup Chip while an agent turn runs
# and taken off when the turn ends, and by `px claim` / `px close`; an agent that
# removes it mid-turn (33GOD-69, 2026-09-23) erases the board's only "someone is
# on this" signal. PLANE_RESERVED_LABELS overrides the list (comma-separated
# names); an empty value turns the guard off.
RESERVED_LABELS_ENV = "PLANE_RESERVED_LABELS"
DEFAULT_RESERVED_LABELS = ("agent:working",)


def reserved_label_names() -> set[str]:
    raw = os.environ.get(RESERVED_LABELS_ENV)
    names = DEFAULT_RESERVED_LABELS if raw is None else raw.split(",")
    return {name.strip().casefold() for name in names if name.strip()}


ACTIONS = (
    Action(
        "list",
        optional=("project_id", "pql", *QUERY_FIELDS),
        note="omit project_id to search the whole workspace",
        read=True,
    ),
    Action("list_archived", ("project_id",), ("pql", *QUERY_FIELDS), read=True),
    Action(
        "retrieve",
        ("project_id", "workitem_id"),
        ("expand", "fields", "external_id", "external_source", "order_by"),
        read=True,
    ),
    Action(
        "retrieve_by_identifier",
        ("workitem_identifier",),
        ("expand", "fields", "external_id", "external_source", "order_by"),
        note="identifier is PROJECT-N, e.g. ENG-42",
        read=True,
    ),
    Action("search", ("query",), ("expand", "fields", "external_id", "external_source", "order_by"), read=True),
    Action(
        "count",
        optional=("project_id", "pql", "group_by", "sub_group_by"),
        note="counts the whole workspace unless project_id narrows it",
        read=True,
    ),
    Action("create", ("project_id", "name"), WRITE_FIELDS[1:]),
    Action(
        "update",
        ("project_id", "workitem_id"),
        tuple(field for field in WRITE_FIELDS if field not in SET_FIELDS),
        note="only the fields you pass are changed; labels and assignees change only through manage_label "
        "and manage_assignee",
    ),
    Action("delete", ("project_id", "workitem_id"), destructive=True),
    Action(
        "archive",
        ("project_id", "workitem_id"),
        ("archive",),
        note="archive defaults to true; pass archive=false to unarchive. Only completed or "
        "cancelled items can be archived",
    ),
    Action(
        "manage_assignee",
        ("project_id", "workitem_id"),
        ("add_user_id", "remove_user_id"),
        note="each takes one id or several; the list is merged, not replaced, and removals apply first",
    ),
    Action(
        "manage_label",
        ("project_id", "workitem_id"),
        ("add_label_id", "remove_label_id"),
        note="each takes one id or several; the list is merged, not replaced, and removals apply first. "
        "Leave agent:working alone: the pipeline puts it on and takes it off",
    ),
)

GROUP_BY_VALUES = (
    "state_id",
    "state__group",
    "priority",
    "project_id",
    "type_id",
    "labels__id",
    "assignees__id",
    "issue_module__module_id",
    "release_work_items__release_id",
    "cycle_id",
    "milestone_id",
    "created_by",
    "target_date",
    "start_date",
)

FOOTER = (
    f"priority: {', '.join(PRIORITIES)}.\n"
    "UUID fields (assignees, labels, state, parent, type_id) need UUIDs -- list the relevant "
    "resource first if you only have a name.\n"
    "description_stripped is plain text and is wrapped into HTML on save; description_html wins "
    "if both are given.\n"
    "fields is a sparse fieldset: use `project`, not project_id, and `description_html`, not "
    "description.\n"
    f"count group_by and sub_group_by accept: {', '.join(GROUP_BY_VALUES)}. These are grouping "
    "keys only -- they are not PQL filter fields, and filtering on state__group is rejected."
)

LEGACY = {
    "list_work_items": "list",
    "list_archived_work_items": "list_archived",
    "retrieve_work_item": "retrieve",
    "retrieve_work_item_by_identifier": "retrieve_by_identifier",
    "search_work_items": "search",
    "count_work_items": "count",
    "create_work_item": "create",
    "update_work_item": "update",
    "delete_work_item": "delete",
    "manage_work_item_archive": "archive",
    "manage_work_item_assignee": "manage_assignee",
    "manage_work_item_label": "manage_label",
}


def _scoped_pql(pql: str, project_id: str) -> str:
    """Narrow a PQL filter to one project, since the count endpoint is workspace-wide."""
    if not project_id:
        return pql
    scope = f'project = "{project_id}"'
    return f"({pql}) AND {scope}" if pql else scope


def _description_html(description_html: str, description_stripped: str) -> str | None:
    """Plane recomputes the stripped form server-side, so plain text must be wrapped."""
    if description_html:
        return description_html
    if description_stripped:
        return "<p>" + escape(description_stripped).replace("\n", "<br/>") + "</p>"
    return None


def _reserved_label_refusal(
    client: Any, workspace_slug: str, project_id: str, adding: list[str], removing: list[str]
) -> str | None:
    """Refuse a manage_label call that would add or remove a pipeline-owned label.

    Label names are looked up here, before the item is read, so the guard does
    not widen the read-modify-write window.
    """
    reserved = reserved_label_names()
    if not reserved:
        return None
    for label_id in dict.fromkeys([*adding, *removing]):
        label = client.labels.retrieve(workspace_slug=workspace_slug, project_id=project_id, label_id=label_id)
        name = getattr(label, "name", None)
        if isinstance(name, str) and name.strip().casefold() in reserved:
            verb = "remove" if label_id in removing else "add"
            return (
                f"Error: manage_label will not {verb} '{name.strip()}'. The pipeline owns it: the Ticket Pickup "
                "Chip puts it on while an agent turn runs and takes it off when the turn ends, and px claim / "
                "px close set it for a claimed ticket. Leave it as it is; nothing was changed."
            )
    return None


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name=NAME,
        description=build_description("Work items -- issues, tasks and epics.", ACTIONS, FOOTER),
        annotations=build_annotations(TITLE, ACTIONS),
    )
    def workitem(  # noqa: PLR0911, PLR0912 - one branch per action is the point
        action: Literal[
            "list",
            "list_archived",
            "retrieve",
            "retrieve_by_identifier",
            "search",
            "count",
            "create",
            "update",
            "delete",
            "archive",
            "manage_assignee",
            "manage_label",
        ],
        project_id: str = "",
        workitem_id: str = "",
        workitem_identifier: str = "",
        query: str = "",
        pql: Annotated[str, Field(description=PQL_FIELD_HINT)] = "",
        group_by: str = "",
        sub_group_by: str = "",
        name: str = "",
        assignees: list[str] | None = None,
        labels: list[str] | None = None,
        type_id: str = "",
        point: int = 0,
        description_html: str = "",
        description_stripped: str = "",
        priority: str = "",
        start_date: str = "",
        target_date: str = "",
        sort_order: float = 0,
        parent: str = "",
        state: str = "",
        estimate_point: str = "",
        add_user_id: str = "",
        remove_user_id: str = "",
        add_label_id: str = "",
        remove_label_id: str = "",
        external_source: str = "",
        external_id: str = "",
        order_by: str = "",
        expand: str = "",
        fields: str = "",
        cursor: str = "",
        per_page: PerPage = 0,
        # Tri-state: False publishes a draft, unset leaves the flag alone.
        is_draft: bool | None = None,
        archive: bool = True,
    ) -> WorkItem | WorkItemDetail | WorkItemSearch | dict[str, Any] | list[Any] | str | None:
        client, workspace_slug = get_plane_client_context(project_id)

        if error := one_of("priority", priority, PRIORITIES):
            return error
        if error := one_of("group_by", group_by, GROUP_BY_VALUES):
            return error
        if error := one_of("sub_group_by", sub_group_by, GROUP_BY_VALUES):
            return error

        def retrieve_params() -> RetrieveQueryParams:
            return RetrieveQueryParams(
                expand=opt(expand),
                fields=opt(fields),
                external_id=opt(external_id),
                external_source=opt(external_source),
                order_by=opt(order_by),
            )

        def write_payload() -> dict[str, Any]:
            return {
                "name": opt(name),
                "assignees": coerce_list(assignees),
                "labels": coerce_list(labels),
                "type_id": opt(type_id),
                "point": opt(point),
                "description_html": _description_html(description_html, description_stripped),
                "priority": opt(priority),
                "start_date": opt(start_date),
                "target_date": opt(target_date),
                "sort_order": opt(sort_order),
                "is_draft": is_draft,
                "external_source": opt(external_source),
                "external_id": opt(external_id),
                "parent": opt(parent),
                "state": opt(state),
                "estimate_point": opt(estimate_point),
            }

        if action in ("list", "list_archived"):
            if action == "list_archived" and not project_id:
                return missing(action, "project_id")
            params = WorkItemQueryParams(
                pql=opt(pql),
                order_by=opt(order_by),
                per_page=opt(per_page),
                cursor=opt(cursor),
                expand=opt(expand),
                fields=opt(fields),
                external_id=opt(external_id),
                external_source=opt(external_source),
            )
            try:
                if action == "list_archived":
                    response = client.work_items.list_archived(
                        workspace_slug=workspace_slug, project_id=project_id, params=params
                    )
                elif project_id:
                    response: PaginatedWorkItemResponse = client.work_items.list(
                        workspace_slug=workspace_slug, project_id=project_id, params=params
                    )
                else:
                    response = client.work_items.list_workspace(workspace_slug=workspace_slug, params=params)
            except HttpError as exc:
                failure = pql_failure("workitem", action, pql, exc)
                if failure:
                    return failure
                raise
            return envelope(response, opt(fields))

        if action == "count":
            scoped = _scoped_pql(pql, project_id)
            try:
                response = client.work_items.count_workspace(
                    workspace_slug=workspace_slug,
                    params=WorkItemCountQueryParams(
                        pql=opt(scoped), group_by=opt(group_by), sub_group_by=opt(sub_group_by)
                    ),
                )
            except HttpError as exc:
                failure = pql_failure("workitem", action, scoped, exc)
                if failure:
                    return failure
                raise
            return response.model_dump()

        if action == "search":
            if not query:
                return missing(action, "query")
            return client.work_items.search(workspace_slug=workspace_slug, query=query, params=retrieve_params())

        if action == "retrieve_by_identifier":
            if not workitem_identifier:
                return missing(action, "workitem_identifier")
            head, _, sequence = workitem_identifier.rpartition("-")
            if not head or not sequence.isdigit():
                return (
                    f"Error: invalid work item identifier {workitem_identifier!r}. "
                    "Expected PROJECT-N, for example ENG-42."
                )
            return client.work_items.retrieve_by_identifier(
                workspace_slug=resolve_workspace_for_identifier(client, head, workspace_slug),
                project_identifier=head,
                issue_identifier=int(sequence),
                params=retrieve_params(),
            )

        if not project_id:
            return missing(action, "project_id")

        if action == "create":
            if not name:
                return missing(action, "name")
            return client.work_items.create(
                workspace_slug=workspace_slug,
                project_id=project_id,
                data=CreateWorkItem(**write_payload()),
            )

        if not workitem_id:
            return missing(action, "workitem_id")

        if action == "retrieve":
            return client.work_items.retrieve(
                workspace_slug=workspace_slug,
                project_id=project_id,
                work_item_id=workitem_id,
                params=retrieve_params(),
            )

        if action == "update":
            if sent := [name for name, value in (("labels", labels), ("assignees", assignees)) if value]:
                return (
                    f"Error: action 'update' does not take {' or '.join(sent)}. Plane replaces the whole list on "
                    "every write, so a list read earlier erases whatever another writer added since. Use "
                    "manage_label (add_label_id / remove_label_id) or manage_assignee (add_user_id / "
                    "remove_user_id); they merge instead of replacing."
                )
            return client.work_items.update(
                workspace_slug=workspace_slug,
                project_id=project_id,
                work_item_id=workitem_id,
                data=UpdateWorkItem(**write_payload()),
            )

        if action == "delete":
            client.work_items.delete(workspace_slug=workspace_slug, project_id=project_id, work_item_id=workitem_id)
            return None

        if action == "archive":
            operation = client.work_items.archive if archive else client.work_items.unarchive
            operation(workspace_slug=workspace_slug, project_id=project_id, work_item_id=workitem_id)
            return {"workitem_id": workitem_id, "archived": archive}

        # manage_assignee / manage_label: Plane's PATCH replaces the whole list,
        # so this is a read-modify-write. Everything that can be looked up ahead
        # of time is; the item's current set is read immediately before the write
        # and only the requested delta is applied to it, so the window in which a
        # concurrent writer's change can be lost is one round trip wide.
        add, remove, field = (
            (add_user_id, remove_user_id, "assignees")
            if action == "manage_assignee"
            else (add_label_id, remove_label_id, "labels")
        )
        if not add and not remove:
            return missing(action, f"add_{field[:-1]}_id or remove_{field[:-1]}_id")
        # Either side takes one id or several, so adding three assignees is one call.
        adding, removing = coerce_list(add) or [], coerce_list(remove) or []
        if field == "labels" and (
            refusal := _reserved_label_refusal(client, workspace_slug, project_id, adding, removing)
        ):
            return refusal
        current = client.work_items.retrieve(
            workspace_slug=workspace_slug, project_id=project_id, work_item_id=workitem_id
        )
        ids = [value for value in ids_of(getattr(current, field)) if value not in removing]
        ids += [value for value in adding if value not in ids]
        written = client.work_items.update(
            workspace_slug=workspace_slug,
            project_id=project_id,
            work_item_id=workitem_id,
            data=UpdateWorkItem(**{field: ids}),
        )
        after = getattr(written, field, None)
        if isinstance(after, list):
            kept = set(ids_of(after))
            dropped = [value for value in adding if value not in kept]
            stuck = [value for value in removing if value in kept]
            if dropped or stuck:
                return (
                    f"Error: Plane answered the {field} write without the change: "
                    f"{'not added ' + ', '.join(dropped) if dropped else ''}"
                    f"{'; ' if dropped and stuck else ''}"
                    f"{'not removed ' + ', '.join(stuck) if stuck else ''}. Retrieve the item before trying again."
                )
        return written
