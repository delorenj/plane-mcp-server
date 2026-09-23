"""Label and assignee writes apply a delta to a fresh read, never a stale list.

Plane's work item PATCH replaces the whole labels (and assignees) list. On
2026-09-23 a grooming turn on 33GOD-69 took `agent:working` off a ticket the
Ticket Pickup Chip had put it on seconds earlier. These pin the two rules that
keep an agent's label write from erasing a label someone else owns:

- the only label write is a delta (manage_label) re-read immediately before the
  PATCH, so a label added after the caller last looked survives;
- a pipeline-owned label (agent:working) is not the caller's to add or remove.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

CHIP = "chip-label-id"
TRIAGED = "triaged-label-id"


def _labels_named(spy, names: dict[str, str]) -> None:
    """Answer labels.retrieve with one name; tests here touch one label id per call."""
    (name,) = set(names.values())
    spy.returns["labels.retrieve"] = SimpleNamespace(id=next(iter(names)), name=name)


def _written(spy, field: str = "labels") -> list[str]:
    update = next(c for c in spy.recorder.calls if c.method == "work_items.update")
    return getattr(update.kwargs["data"], field)


def test_a_label_added_after_the_caller_read_the_item_survives(registered, spy):
    """The race behind 33GOD-69: read, someone else labels, then write."""
    tool = registered["workitem"].fn
    spy.returns["work_items.retrieve"] = SimpleNamespace(labels=["existing"], assignees=[])
    tool(action="retrieve", project_id="p", workitem_id="w")  # the caller's view: ["existing"]

    # The chip puts agent:working on while the caller is thinking.
    spy.returns["work_items.retrieve"] = SimpleNamespace(labels=["existing", CHIP], assignees=[])
    _labels_named(spy, {TRIAGED: "lifecycle:triaged"})
    spy.recorder.calls.clear()

    tool(action="manage_label", project_id="p", workitem_id="w", add_label_id=TRIAGED)

    assert _written(spy) == ["existing", CHIP, TRIAGED], "the write dropped a label added after the caller's read"


def test_the_item_is_read_immediately_before_the_write(registered, spy):
    """Name lookups come first, so the read-modify-write window is one round trip."""
    spy.returns["work_items.retrieve"] = SimpleNamespace(labels=["existing"], assignees=[])
    _labels_named(spy, {TRIAGED: "lifecycle:triaged"})

    registered["workitem"].fn(action="manage_label", project_id="p", workitem_id="w", add_label_id=TRIAGED)

    methods = spy.recorder.methods
    assert methods[-2:] == ["work_items.retrieve", "work_items.update"], methods
    assert methods.index("labels.retrieve") < methods.index("work_items.retrieve"), methods


def test_removing_a_label_keeps_every_other_label_on_the_fresh_read(registered, spy):
    spy.returns["work_items.retrieve"] = SimpleNamespace(labels=["stale", CHIP, "other"], assignees=[])
    _labels_named(spy, {"stale": "lifecycle:new"})

    registered["workitem"].fn(action="manage_label", project_id="p", workitem_id="w", remove_label_id="stale")

    assert _written(spy) == [CHIP, "other"]


def test_an_assignee_added_after_the_caller_read_the_item_survives(registered, spy):
    spy.returns["work_items.retrieve"] = SimpleNamespace(labels=[], assignees=["claimer"])

    registered["workitem"].fn(action="manage_assignee", project_id="p", workitem_id="w", add_user_id="pm")

    assert _written(spy, "assignees") == ["claimer", "pm"]


@pytest.mark.parametrize(
    "field", [{"labels": ["a", "b"]}, {"assignees": ["u"]}, {"labels": ["a"], "name": "x"}], ids=str
)
def test_update_refuses_a_whole_list(field, registered, spy):
    """A full list is exactly the stale write that erases a concurrent writer's change."""
    result = registered["workitem"].fn(action="update", project_id="p", workitem_id="w", **field)

    assert isinstance(result, str) and result.startswith("Error:"), result
    assert "manage_label" in result and "manage_assignee" in result, result
    assert not spy.recorder.calls, f"reached the SDK: {spy.recorder.methods}"


def test_update_without_a_list_still_writes(registered, spy):
    registered["workitem"].fn(action="update", project_id="p", workitem_id="w", name="renamed")

    assert spy.recorder.only().method == "work_items.update"


def test_update_no_longer_advertises_labels_or_assignees(resource_modules):
    workitem = next(mod for mod in resource_modules if mod.NAME == "workitem")
    update = next(action for action in workitem.ACTIONS if action.name == "update")
    create = next(action for action in workitem.ACTIONS if action.name == "create")

    assert not {"labels", "assignees"} & set(update.optional), update.optional
    assert {"labels", "assignees"} <= set(create.optional), "a new item has nothing to clobber"


@pytest.mark.parametrize("side", ["add_label_id", "remove_label_id"])
@pytest.mark.parametrize("spelling", ["agent:working", " Agent:Working "])
def test_the_pipeline_owned_label_is_refused(side, spelling, registered, spy):
    """33GOD-69: the grooming turn removed agent:working as 'stale'."""
    spy.returns["work_items.retrieve"] = SimpleNamespace(labels=[CHIP], assignees=[])
    _labels_named(spy, {CHIP: spelling})

    result = registered["workitem"].fn(action="manage_label", project_id="p", workitem_id="w", **{side: CHIP})

    assert isinstance(result, str) and result.startswith("Error:"), result
    assert "agent:working" in result.lower(), result
    assert "work_items.update" not in spy.recorder.methods, "wrote despite refusing"
    assert "work_items.retrieve" not in spy.recorder.methods, "read the item for a call it was going to refuse"


def test_a_mixed_call_touching_the_reserved_label_changes_nothing(registered, spy):
    spy.returns["work_items.retrieve"] = SimpleNamespace(labels=[CHIP], assignees=[])
    _labels_named(spy, {CHIP: "agent:working"})

    result = registered["workitem"].fn(
        action="manage_label", project_id="p", workitem_id="w", add_label_id=TRIAGED, remove_label_id=CHIP
    )

    assert isinstance(result, str) and result.startswith("Error:"), result
    assert "work_items.update" not in spy.recorder.methods


def test_the_reserved_list_is_configurable(registered, spy, monkeypatch):
    monkeypatch.setenv("PLANE_RESERVED_LABELS", "")
    spy.returns["work_items.retrieve"] = SimpleNamespace(labels=[CHIP], assignees=[])
    _labels_named(spy, {CHIP: "agent:working"})

    registered["workitem"].fn(action="manage_label", project_id="p", workitem_id="w", remove_label_id=CHIP)

    assert _written(spy) == [], "an empty PLANE_RESERVED_LABELS must turn the guard off"
    assert "labels.retrieve" not in spy.recorder.methods, "looked up names with the guard off"


def test_other_labels_are_not_reserved(registered, spy, monkeypatch):
    monkeypatch.setenv("PLANE_RESERVED_LABELS", "agent:working, pipeline:locked")
    spy.returns["work_items.retrieve"] = SimpleNamespace(labels=[], assignees=[])
    _labels_named(spy, {TRIAGED: "lifecycle:triaged"})

    registered["workitem"].fn(action="manage_label", project_id="p", workitem_id="w", add_label_id=TRIAGED)

    assert _written(spy) == [TRIAGED]


def test_a_write_plane_did_not_keep_is_reported(registered, spy):
    """Plane answers with the item as written; a delta missing from it is not a success."""
    spy.returns["work_items.retrieve"] = SimpleNamespace(labels=["existing"], assignees=[])
    spy.returns["work_items.update"] = SimpleNamespace(labels=["existing"], assignees=[])
    _labels_named(spy, {TRIAGED: "lifecycle:triaged"})

    result = registered["workitem"].fn(action="manage_label", project_id="p", workitem_id="w", add_label_id=TRIAGED)

    assert isinstance(result, str) and result.startswith("Error:"), result
    assert TRIAGED in result, result


def test_a_write_plane_kept_returns_the_item(registered, spy):
    written = SimpleNamespace(labels=["existing", TRIAGED], assignees=[])
    spy.returns["work_items.retrieve"] = SimpleNamespace(labels=["existing"], assignees=[])
    spy.returns["work_items.update"] = written
    _labels_named(spy, {TRIAGED: "lifecycle:triaged"})

    result = registered["workitem"].fn(action="manage_label", project_id="p", workitem_id="w", add_label_id=TRIAGED)

    assert result is written
