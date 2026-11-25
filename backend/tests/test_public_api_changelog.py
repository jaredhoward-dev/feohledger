"""The public contract's change notes and webhook docs agree with the code.

`app/api/v1_changelog.PUBLIC_API_CHANGES` is the one list of dated change notes
(decisions §548). Pinned here:

* it is newest first, ids unique, and names only catalogued event types;
* the published spec carries it as `info.x-changelog`, `info.version` stays the
  path prefix, and `/api/v1/docs` renders every entry;
* `backend/docs/public-api.md` § Changes has exactly one entry per note, by id
  and date, and names its events;
* the doc's event catalogue names exactly `WEBHOOK_EVENT_TYPES`, and every
  type has one payload example whose `data` keys are its model's fields;
* `missing_subscriptions` (behind `scripts/public_api_change_report.py`) picks
  only an affected subscription, and only the types it lacks.

Pure: no DB.
"""

from __future__ import annotations

import pathlib
import re
from datetime import date

import pytest

from app.api.v1_changelog import (
    PUBLIC_API_CHANGES,
    PublicApiChange,
    changelog_spec,
    missing_subscriptions,
)
from app.api.v1_openapi import PUBLIC_API_VERSION, build_public_openapi, render_docs_html
from app.main import app
from app.models.webhook import (
    EVENT_EXCEPTION_RAISED,
    EVENT_INVOICE_APPROVED,
    EVENT_INVOICE_ERP_BILL_VOIDED,
    EVENT_PAYMENT_VOIDED,
    WEBHOOK_EVENT_TYPES,
)
from app.schemas.webhook_events import WEBHOOK_EVENT_DATA_MODELS

_DOC = pathlib.Path(__file__).resolve().parents[1] / "docs/public-api.md"


def _section(text: str, heading: str) -> str:
    """The body of a markdown section, up to the next heading of its level."""
    level = heading.split(" ", 1)[0]
    start = text.index(f"\n{heading}\n") + len(heading) + 2
    match = re.compile(rf"^{level} ", re.M).search(text, start)
    return text[start : match.start() if match else len(text)]


# ── The list itself ─────────────────────────────────────────────────────────


def test_the_changes_are_newest_first_unique_and_name_catalogued_events():
    assert PUBLIC_API_CHANGES, "the list must not be empty once a change is announced"
    dates = [c.on for c in PUBLIC_API_CHANGES]
    assert dates == sorted(dates, reverse=True)
    ids = [c.id for c in PUBLIC_API_CHANGES]
    assert len(ids) == len(set(ids))
    for change in PUBLIC_API_CHANGES:
        assert change.id.startswith(change.on.isoformat())
        assert set(change.affects) <= set(WEBHOOK_EVENT_TYPES)
        assert set(change.subscribe_to) <= set(WEBHOOK_EVENT_TYPES)


def test_the_void_events_change_is_announced():
    """Decisions §542: subscribers to `invoice.approved` are told to add the two
    void events, which now replace the approval a void used to send."""
    (change,) = [c for c in PUBLIC_API_CHANGES if c.id == "2026-11-25-void-events"]
    assert change.affects == (EVENT_INVOICE_APPROVED,)
    assert change.subscribe_to == (EVENT_PAYMENT_VOIDED, EVENT_INVOICE_ERP_BILL_VOIDED)


# ── Where it is published ───────────────────────────────────────────────────


def test_the_spec_carries_the_changelog_and_keeps_its_version():
    info = build_public_openapi(app)["info"]
    assert info["x-changelog"] == changelog_spec()
    assert info["version"] == PUBLIC_API_VERSION == "v1"


def test_the_docs_page_renders_every_change():
    page = render_docs_html(build_public_openapi(app))
    assert "<h2>Changes</h2>" in page
    for change in PUBLIC_API_CHANGES:
        assert f"<h3>{change.on.isoformat()}: {change.title}</h3>" in page
        for event in change.subscribe_to:
            assert f"<code>{event}</code>" in page


def test_the_docs_page_omits_changes_when_there_are_none():
    assert "Changes" not in render_docs_html({"info": {"title": "t", "x-changelog": []}})


def test_the_doc_lists_exactly_the_published_changes():
    changes = _section(_DOC.read_text(), "## Changes")
    headings = re.findall(r"^### (\d{4}-\d{2}-\d{2}): (.+)$", changes, re.M)
    assert headings == [(c.on.isoformat(), c.title) for c in PUBLIC_API_CHANGES]
    for change in PUBLIC_API_CHANGES:
        body = _section(changes, f"### {change.on.isoformat()}: {change.title}")
        assert f"Id `{change.id}`" in body
        for event in (*change.affects, *change.subscribe_to):
            assert f"`{event}`" in body


# ── The doc's webhook catalogue and payloads ────────────────────────────────


def test_the_doc_catalogue_names_exactly_the_event_types():
    catalog = _section(_DOC.read_text(), "### Event catalog").split("(see", 1)[0]
    assert re.findall(r"`([a-z_]+\.[a-z_]+)`", catalog) == list(WEBHOOK_EVENT_TYPES)


def _payload_examples() -> dict[str, list[set[str]]]:
    examples: dict[str, list[set[str]]] = {}
    for block in re.findall(r"```jsonc\n(.*?)```", _DOC.read_text(), re.S):
        kind = re.search(r'"type": "([^"]+)"', block)
        data = re.search(r'"data": \{(.*)\}\s*\}', block, re.S)
        if kind and data:
            keys = set(re.findall(r'"([a-z_]+)":', data.group(1)))
            examples.setdefault(kind.group(1), []).append(keys)
    return examples


@pytest.mark.parametrize("event_type", WEBHOOK_EVENT_TYPES)
def test_every_event_has_one_payload_example_with_its_models_fields(event_type):
    examples = _payload_examples()
    assert len(examples.get(event_type, [])) == 1, f"one example for {event_type}"
    assert examples[event_type][0] == set(WEBHOOK_EVENT_DATA_MODELS[event_type].model_fields)


def test_no_payload_example_names_an_uncatalogued_event():
    assert set(_payload_examples()) <= set(WEBHOOK_EVENT_TYPES)


# ── The operator report's filter ────────────────────────────────────────────


_CHANGE = PublicApiChange(
    id="2026-01-01-x",
    on=date(2026, 1, 1),
    title="t",
    detail="d",
    affects=(EVENT_INVOICE_APPROVED,),
    subscribe_to=(EVENT_PAYMENT_VOIDED, EVENT_INVOICE_ERP_BILL_VOIDED),
)


@pytest.mark.parametrize(
    ("event_types", "missing"),
    [
        ([EVENT_INVOICE_APPROVED], [EVENT_PAYMENT_VOIDED, EVENT_INVOICE_ERP_BILL_VOIDED]),
        ([EVENT_INVOICE_APPROVED, EVENT_PAYMENT_VOIDED], [EVENT_INVOICE_ERP_BILL_VOIDED]),
        (
            [EVENT_INVOICE_APPROVED, EVENT_PAYMENT_VOIDED, EVENT_INVOICE_ERP_BILL_VOIDED],
            [],
        ),
        ([EVENT_EXCEPTION_RAISED], []),  # not affected: never subscribed to the approval
        ([], []),
        (None, []),
    ],
)
def test_missing_subscriptions(event_types, missing):
    assert missing_subscriptions(_CHANGE, event_types) == missing


def test_the_report_script_resolves_a_change_without_a_database():
    import importlib.util

    path = pathlib.Path(__file__).resolve().parents[1] / "scripts/public_api_change_report.py"
    spec = importlib.util.spec_from_file_location("public_api_change_report", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.find_change(None) is PUBLIC_API_CHANGES[0]
    assert module.find_change(PUBLIC_API_CHANGES[0].id) is PUBLIC_API_CHANGES[0]
    assert module.find_change("no-such-change") is None
    assert module.main(["--change", "no-such-change"]) == module.EXIT_UNKNOWN_CHANGE
