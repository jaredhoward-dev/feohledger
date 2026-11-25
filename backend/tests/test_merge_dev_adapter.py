"""Merge.dev's bill push against Merge's published write contract (decisions §399).

Checked against https://docs.merge.dev/openapi/accounting.json and Merge's
write-error reference: a bill line's amount is `total_amount`, the create
answers `{model, warnings, errors}` with each error's `problem_type`, and only
that code (never `detail`) decides what the refusal is. The idempotency
lookup and header are in `test_erp_adapter_idempotency.py`, the exact-money
encoding in `test_erp_adapter_money_exact.py`, the webhook in
`test_merge_webhook.py`.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.erp_adapters.base import InvoicePayload, LineItemPayload
from app.services.erp_adapters.merge_dev import MergeDevAdapter, invoice_sync_covers


def _run(coro):
    return asyncio.run(coro)


def _resp(status: int, body: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json = MagicMock(return_value=body if body is not None else {})
    return resp


def _payload(**overrides) -> InvoicePayload:
    base = dict(
        invoice_number="INV-7",
        vendor_name="Acme",
        amount=Decimal("120.00"),
        currency="USD",
        invoice_date=date(2026, 11, 1),
        correlation_id="corr-merge-7",
        vendor_erp_id="merge-vendor-701",
        gl_account="6100",
        gl_account_erp_id="merge-acct-6100",
        line_items=[
            LineItemPayload(
                line_number=1,
                description="Paper",
                total=Decimal("100.00"),
                gl_account="6100",
                gl_account_erp_id="merge-acct-6100",
            ),
            LineItemPayload(
                line_number=2,
                description="Licence",
                total=Decimal("20.00"),
                gl_account="6200",
                gl_account_erp_id="merge-acct-6200",
            ),
        ],
    )
    base.update(overrides)
    return InvoicePayload(**base)


_NO_BILL = _resp(200, {"next": None, "results": []})
_ADAPTER = {"api_key": "k", "account_token": "tok"}


def _push(create: MagicMock, *, reads: dict[str, MagicMock] | None = None, payload=None):
    """Post through the adapter: the lookup finds nothing, the create answers
    ``create``, and a record read (``contacts/…`` / ``accounts/…``) answers
    from ``reads`` (404 when absent)."""
    reads = reads or {}

    async def get(url, **kwargs):
        if url.endswith("/invoices"):
            return _NO_BILL
        for suffix, resp in reads.items():
            if url.endswith(suffix):
                return resp
        return _resp(404)

    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(side_effect=get)
        client.post = AsyncMock(return_value=create)
        result = _run(MergeDevAdapter(_ADAPTER).post_invoice(payload or _payload()))
    return result, client


def _refused(*problems: str, status: int = 400) -> MagicMock:
    return _resp(
        status,
        {"model": None, "errors": [{"problem_type": p, "detail": "x"} for p in problems]},
    )


# ---------------------------------------------------------------------------
# what is sent
# ---------------------------------------------------------------------------


def test_each_line_is_sent_with_its_amount_as_total_amount_on_its_own_account():
    created = _resp(201, {"model": {"id": "m1", "number": "INV-7", "total_amount": 120.00}})
    result, client = _push(created)
    assert result.success and result.erp_document_id == "m1"
    model = json.loads(client.post.await_args.kwargs["content"])["model"]
    assert model["line_items"] == [
        {"description": "Paper", "total_amount": 100.00, "account": "merge-acct-6100"},
        {"description": "Licence", "total_amount": 20.00, "account": "merge-acct-6200"},
    ]
    assert model["total_amount"] == 120.00 and model["inclusive_of_tax"] is True
    # Fields Merge's Invoice does not have, or a marker it never echoes back.
    for absent in ("purchase_order_number", "integration_params", "sub_total"):
        assert absent not in model


@pytest.mark.parametrize(
    ("lines", "reason"),
    [
        # Lines that do not add up to the approved amount: never posted.
        (
            [LineItemPayload(line_number=1, total=Decimal("99.00"), gl_account_erp_id="a")],
            "amount_mismatch",
        ),
        # A line with no amount and nothing to derive one from.
        ([LineItemPayload(line_number=1, gl_account_erp_id="a")], "line_amount_missing"),
        # A coded line whose account never synced.
        (
            [LineItemPayload(line_number=1, total=Decimal("120.00"), gl_account="6900")],
            "account_not_linked",
        ),
    ],
)
def test_a_bill_whose_lines_cannot_be_stated_is_refused_before_any_call(lines, reason):
    with patch("httpx.AsyncClient") as cm:
        result = _run(MergeDevAdapter(_ADAPTER).post_invoice(_payload(line_items=lines)))
    assert result.refusal_code == reason and result.not_posted and not result.retryable
    cm.assert_not_called()


# ---------------------------------------------------------------------------
# what a 2xx means
# ---------------------------------------------------------------------------


def test_a_posted_total_other_than_ours_is_never_success():
    result, _ = _push(_resp(201, {"model": {"id": "m1", "total_amount": 132.00}}))
    assert not result.success and not result.retryable
    assert result.refusal_code == "posted_total_mismatch"
    assert result.erp_document_id == "m1"  # Merge cannot void: left for a person


def test_a_created_model_with_no_total_is_unconfirmed():
    result, _ = _push(_resp(201, {"model": {"id": "m1"}}))
    assert not result.success and result.refusal_code == "posted_total_unconfirmed"


def test_a_2xx_with_no_model_id_is_not_a_confirmed_bill():
    result, _ = _push(_resp(201, {"model": {}, "errors": []}))
    assert not result.success and not result.retryable
    assert result.refusal_code == "bill_refused"
    assert result.not_posted is False  # nothing says it was not created


def test_a_2xx_carrying_errors_keeps_the_model_id_it_names():
    result, _ = _push(
        _resp(201, {"model": {"id": "m9"}, "errors": [{"problem_type": "PROVIDER_ERROR"}]})
    )
    assert result.refusal_code == "bill_refused" and result.not_posted is False
    assert result.erp_document_id == "m9"


# ---------------------------------------------------------------------------
# what a refusal means
# ---------------------------------------------------------------------------


def test_a_401_is_not_connected_and_not_retried():
    result, _ = _push(_resp(401, {"detail": "Invalid token"}))
    assert result.refusal_code == "not_connected"
    assert not result.retryable and result.not_posted


@pytest.mark.parametrize("problem", ["RELINK_NEEDED", "CREDENTIAL_REFRESH_FAILED"])
def test_a_link_that_needs_relinking_is_not_connected(problem):
    result, _ = _push(_refused(problem))
    assert result.refusal_code == "not_connected" and result.not_posted
    assert result.refusal_params == {"merge_problem": problem}


@pytest.mark.parametrize(
    "problem", ["DUPLICATE_MODEL_EXISTS", "MODEL_WITH_DUPLICATE_UNIQUE_FIELD_EXISTS"]
)
def test_a_duplicate_is_coded_but_stays_possibly_posted(problem):
    """The record holding the number may be our own earlier attempt, which
    Merge's synced copy did not yet show the lookup."""
    result, _ = _push(_refused(problem))
    assert result.refusal_code == "duplicate_document_number"
    assert result.not_posted is False and not result.retryable


def test_a_provider_error_names_an_archived_vendor():
    result, client = _push(
        _refused("PROVIDER_ERROR"),
        reads={"contacts/merge-vendor-701": _resp(200, {"id": "v", "status": "ARCHIVED"})},
    )
    assert result.refusal_code == "vendor_inactive" and result.not_posted


def test_a_provider_error_names_each_inactive_account_by_our_code():
    result, _ = _push(
        _refused("PROVIDER_ERROR"),
        reads={
            "contacts/merge-vendor-701": _resp(200, {"status": "ACTIVE"}),
            "accounts/merge-acct-6200": _resp(200, {"status": "INACTIVE"}),
            "accounts/merge-acct-6100": _resp(200, {"status": "ACTIVE"}),
        },
    )
    assert result.refusal_code == "account_inactive" and result.not_posted
    assert result.refusal_params == {"merge_problem": "PROVIDER_ERROR", "accounts": ["6200"]}


def test_a_provider_error_with_nothing_inactive_or_unreadable_is_bill_refused():
    """An unreadable record is "don't know", never "inactive". And a bare
    PROVIDER_ERROR is the integration's catch-all, which says nothing about
    whether a multi-step write left a bill: kept possibly posted."""
    result, _ = _push(_refused("PROVIDER_ERROR"))
    assert result.refusal_code == "bill_refused" and result.not_posted is False


def test_a_documented_not_created_problem_is_bill_refused():
    result, _ = _push(_refused("MISSING_REQUIRED_FIELD", "INVALID_FIELD_CHOICE"))
    assert result.refusal_code == "bill_refused" and result.not_posted
    assert result.refusal_params == {"merge_problem": "MISSING_REQUIRED_FIELD"}


@pytest.mark.parametrize(
    "create",
    [
        _refused("ERROR"),  # generic: says nothing about whether it landed
        _refused("MISSING_REQUIRED_FIELD", "SOMETHING_UNDOCUMENTED"),
        _resp(400, {"detail": "no errors array"}),
    ],
)
def test_an_undocumented_or_absent_problem_stays_possibly_posted(create):
    result, _ = _push(create)
    assert not result.success and not result.retryable
    assert result.refusal_code is None and result.not_posted is False
    assert result.message.startswith("Merge.dev post failed: HTTP 400")


def test_merges_rate_limit_stays_possibly_posted():
    """Whether Merge ever answers 429 after the write reached the integration
    is unverified (#654), so a 429 is an unknown outcome like a 5xx: not
    retried automatically, and a person's retry waits for a sync (§409)."""
    result, _ = _push(_refused("RATE_LIMITED", status=429))
    assert not result.success and not result.retryable and result.not_posted is False


@pytest.mark.parametrize("status", [500, 502, 503, 408])
def test_a_create_that_may_have_landed_is_not_retried_automatically(status):
    """The automatic retry would come within seconds, before Merge's listing
    may show a bill written through it, so the lookup could miss it and post a
    second one (the Idempotency-Key beta may be off). Possibly posted; a
    person's retry runs the lookup again (decisions §399)."""
    result, _ = _push(_refused("ERROR", status=status))
    assert not result.success and not result.retryable and result.not_posted is False


@pytest.mark.parametrize("exc", ["ReadTimeout", "RemoteProtocolError", "WriteTimeout"])
def test_a_lost_create_response_is_not_retried_automatically(exc):
    import httpx

    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_NO_BILL)
        client.post = AsyncMock(side_effect=getattr(httpx, exc)("t"))
        result = _run(MergeDevAdapter(_ADAPTER).post_invoice(_payload()))
    assert not result.success and not result.retryable and result.not_posted is False
    assert "outcome unknown" in result.message


@pytest.mark.parametrize("exc", ["ConnectError", "ConnectTimeout"])
def test_a_create_that_never_connected_is_retried(exc):
    import httpx

    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_NO_BILL)
        client.post = AsyncMock(side_effect=getattr(httpx, exc)("t"))
        result = _run(MergeDevAdapter(_ADAPTER).post_invoice(_payload()))
    assert not result.success and result.retryable


def test_a_401_on_the_lookup_is_not_connected_before_anything_is_sent():
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_resp(401))
        client.post = AsyncMock()
        result = _run(MergeDevAdapter(_ADAPTER).post_invoice(_payload()))
    assert result.refusal_code == "not_connected" and result.not_posted
    client.post.assert_not_awaited()


# ---------------------------------------------------------------------------
# the connection test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "body", "ok"),
    [
        (200, {"status": "COMPLETE"}, True),
        (200, {"status": "IDLE"}, True),
        (200, {}, True),
        # Merge answers 200 for a link that cannot write.
        (200, {"status": "RELINK_NEEDED"}, False),
        (200, {"status": "INCOMPLETE"}, False),
        (401, {}, False),
    ],
)
def test_the_connection_test_reads_the_linked_accounts_status(status, body, ok):
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_resp(status, body))
        assert _run(MergeDevAdapter(_ADAPTER).test_connection()) is ok


# ---------------------------------------------------------------------------
# an unknown region, outside the push
# ---------------------------------------------------------------------------


def test_an_unknown_region_fails_a_listing_as_incomplete_not_a_500():
    from app.services.erp_adapters.base import ErpListingIncomplete

    adapter = MergeDevAdapter({**_ADAPTER, "region": "mars"})
    for listing in (adapter.list_vendors, adapter.list_gl_accounts, adapter.list_pos_report):
        with patch("httpx.AsyncClient") as cm, pytest.raises(ErpListingIncomplete):
            _run(listing())
        cm.assert_not_called()


def test_an_unknown_region_reads_status_unknown():
    from app.services.erp_adapters.base import ErpInvoiceStatus

    with patch("httpx.AsyncClient") as cm:
        status = _run(MergeDevAdapter({**_ADAPTER, "region": "mars"}).get_invoice_status("b"))
    assert status is ErpInvoiceStatus.unknown
    cm.assert_not_called()


# ---------------------------------------------------------------------------
# the bill read the webhook makes
# ---------------------------------------------------------------------------


def test_bill_report_reads_the_bill_back_from_merge():
    from app.services.erp_adapters.base import ErpBillState

    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_resp(200, {"id": "b1", "status": "VOID"}))
        report = _run(MergeDevAdapter(_ADAPTER).bill_report("b1"))
    assert report.state is ErpBillState.deleted and report.bill_id == "b1"
    assert client.get.await_args.args[0].endswith("/invoices/b1")


def test_bill_report_never_reads_an_unreadable_bill_as_gone():
    from app.services.erp_adapters.merge_dev import MergeBillNotFound

    adapter = MergeDevAdapter(_ADAPTER)
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_resp(404))
        with pytest.raises(MergeBillNotFound):
            _run(adapter.bill_report("b1"))
        client.get = AsyncMock(return_value=_resp(500))
        with pytest.raises(RuntimeError):
            _run(adapter.bill_report("b1"))
        # A body for another bill is not this bill's state.
        client.get = AsyncMock(return_value=_resp(200, {"id": "b2", "status": "VOID"}))
        with pytest.raises(RuntimeError):
            _run(adapter.bill_report("b1"))


# ---------------------------------------------------------------------------
# a retry after an attempt whose outcome is unknown waits for Merge's sync
# (decisions §405, GET /sync-status read 2026-11-14)
# ---------------------------------------------------------------------------

_SINCE = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)
_AFTER = "2026-11-14T10:10:01Z"  # just past SYNC_SETTLE_MARGIN (10 min)
_BEFORE = "2026-11-14T09:55:00Z"


def _sync_row(model: str = "Invoice", **fields) -> dict:
    row = {"model_name": model, "model_id": f"accounting.{model}", "status": "DONE"}
    row.update(fields)
    return row


def _sync_status(*rows: dict, next_cursor: str | None = None) -> MagicMock:
    return _resp(200, {"next": next_cursor, "previous": None, "results": list(rows)})


@pytest.mark.parametrize(
    ("row", "covers"),
    [
        # Merge's own answer: complete through the last successful sync's start.
        (_sync_row(data_fresh_as_of=_AFTER), True),
        (_sync_row(data_fresh_as_of=_BEFORE), False),
        # Inside the margin: Merge may still have been writing the bill (§409).
        (_sync_row(data_fresh_as_of="2026-11-14T10:00:01Z"), False),
        (_sync_row(data_fresh_as_of="2026-11-14T10:05:00Z"), False),
        (_sync_row(last_sync_result="DONE", last_sync_start="2026-11-14T10:05:00Z"), False),
        # Exactly at the margin's end cannot be shown to see it either.
        (_sync_row(data_fresh_as_of="2026-11-14T10:10:00Z"), False),
        # Null: no sync has completed.
        (_sync_row(data_fresh_as_of=None, last_sync_result="DONE", last_sync_start=_AFTER), False),
        # Without the field: a link at rest whose last sync is DONE and began after.
        (_sync_row(last_sync_result="DONE", last_sync_start=_AFTER), True),
        (_sync_row(last_sync_result="DONE", last_sync_start=_BEFORE), False),
        # While SYNCING, last_sync_start is the running sync's.
        (_sync_row(status="SYNCING", last_sync_result="DONE", last_sync_start=_AFTER), False),
        (_sync_row(last_sync_result="FAILED", last_sync_start=_AFTER), False),
        (_sync_row(last_sync_result="PARTIALLY_SYNCED", last_sync_start=_AFTER), False),
        # A time with no zone cannot be ordered against ours.
        (_sync_row(data_fresh_as_of="2026-11-14T10:10:01"), False),
        (_sync_row(), False),
    ],
)
def test_invoice_sync_covers(row, covers):
    assert invoice_sync_covers(row, _SINCE) is covers


def _retry(
    sync: MagicMock | Exception,
    lookup: MagicMock = _NO_BILL,
    *,
    create: MagicMock | None = None,
):
    """A push after an attempt whose outcome is unknown: ``sync`` answers
    ``GET /sync-status`` (or is raised), ``lookup`` the pre-create lookup."""
    calls: list[str] = []

    async def get(url, **kwargs):
        calls.append(url.rsplit("/", 1)[-1])
        if url.endswith("/sync-status"):
            if isinstance(sync, Exception):
                raise sync
            return sync
        if url.endswith("/invoices"):
            return lookup
        return _resp(404)

    created = create or _resp(201, {"model": {"id": "m2", "number": "INV-7", "total_amount": 120}})
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(side_effect=get)
        client.post = AsyncMock(return_value=created)
        result = _run(
            MergeDevAdapter(_ADAPTER).post_invoice(_payload(outcome_unknown_since=_SINCE))
        )
    return result, client, calls


def test_a_retry_after_a_sync_that_covers_the_attempt_creates_the_bill():
    result, client, calls = _retry(_sync_status(_sync_row(data_fresh_as_of=_AFTER)))
    assert result.success and result.erp_document_id == "m2"
    # Asked before the lookup, so the listing the lookup reads is that fresh.
    assert calls == ["sync-status", "invoices"]
    assert client.post.await_count == 1


def test_a_retry_before_merge_has_synced_is_refused_coded_and_sends_nothing():
    result, client, _calls = _retry(_sync_status(_sync_row(data_fresh_as_of=_BEFORE)))
    assert not result.success
    assert result.refusal_code == "merge_sync_pending"
    assert result.not_posted is True and result.retryable is False
    client.post.assert_not_awaited()


def test_a_retry_before_merge_has_synced_still_finds_the_bill_already_listed():
    """A listing that already shows the bill needs no wait: the bill is ours."""
    listed = _resp(
        200,
        {
            "next": None,
            "results": [
                {
                    "id": "m1",
                    "number": "INV-7",
                    "total_amount": 120,
                    "currency": "USD",
                    "status": "OPEN",
                }
            ],
        },
    )
    result, client, _calls = _retry(_sync_status(_sync_row(data_fresh_as_of=_BEFORE)), listed)
    assert result.success and result.erp_document_id == "m1"
    client.post.assert_not_awaited()


def test_a_sync_status_that_does_not_list_invoices_is_not_yet():
    result, client, _calls = _retry(_sync_status(_sync_row("Contact", data_fresh_as_of=_AFTER)))
    assert result.refusal_code == "merge_sync_pending"
    client.post.assert_not_awaited()


def test_the_invoice_row_is_found_on_a_later_page():
    pages = iter(
        [
            _sync_status(_sync_row("Account", data_fresh_as_of=_AFTER), next_cursor="c2"),
            _sync_status(_sync_row(data_fresh_as_of=_AFTER)),
        ]
    )
    seen_params: list[dict] = []

    async def get(url, **kwargs):
        if url.endswith("/sync-status"):
            seen_params.append(dict(kwargs.get("params") or {}))
            return next(pages)
        return _NO_BILL

    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(side_effect=get)
        client.post = AsyncMock(
            return_value=_resp(201, {"model": {"id": "m2", "total_amount": 120}})
        )
        result = _run(
            MergeDevAdapter(_ADAPTER).post_invoice(_payload(outcome_unknown_since=_SINCE))
        )
    assert result.success
    assert seen_params[1].get("cursor") == "c2"


@pytest.mark.parametrize(
    "sync",
    [_resp(503), _resp(200, {"results": "nope"}), "ReadTimeout"],
)
def test_an_unreadable_sync_status_sends_nothing_and_may_be_retried(sync):
    import httpx

    if isinstance(sync, str):
        sync = getattr(httpx, sync)("t")
    result, client, calls = _retry(sync)
    assert not result.success and result.retryable and result.not_posted is True
    assert "sync_status_unavailable" in result.message
    assert calls == ["sync-status"]
    client.post.assert_not_awaited()


def test_a_401_on_sync_status_is_not_connected():
    result, client, _calls = _retry(_resp(401))
    assert result.refusal_code == "not_connected" and result.not_posted is True
    client.post.assert_not_awaited()


def test_a_first_attempt_never_asks_for_the_sync_status():
    result, client = _push(_resp(201, {"model": {"id": "m1", "total_amount": 120}}))
    assert result.success
    assert not any(c.args[0].endswith("/sync-status") for c in client.get.await_args_list)


@pytest.mark.parametrize("exc", ["ConnectError", "ConnectTimeout"])
def test_a_create_that_never_connected_sent_nothing(exc):
    """Not an attempt whose outcome is unknown, so its retry waits for no sync."""
    import httpx

    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_NO_BILL)
        client.post = AsyncMock(side_effect=getattr(httpx, exc)("t"))
        result = _run(MergeDevAdapter(_ADAPTER).post_invoice(_payload()))
    assert result.not_posted is True


# ---------------------------------------------------------------------------
# tenant deletion deletes the linked account (decisions §405)
# ---------------------------------------------------------------------------


def test_the_provider_side_step_names_the_webhook_its_key_and_the_api_key():
    step = MergeDevAdapter.provider_side_disconnect
    assert MergeDevAdapter.revokes_stored_access is True
    for needed in ("webhook", "signature key", "API key", "linked account"):
        assert needed in step


def test_revoke_deletes_the_linked_account_with_its_own_token():
    from app.services.erp_oauth import REVOKED

    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=_resp(200))
        outcome = _run(MergeDevAdapter({**_ADAPTER, "region": "EU"}).revoke_stored_access())
    assert outcome == REVOKED
    (call,) = client.post.await_args_list
    assert call.args[0] == "https://api-eu.merge.dev/api/accounting/v1/delete-account"
    assert call.kwargs["headers"]["X-Account-Token"] == "tok"
    assert call.kwargs["headers"]["Authorization"] == "Bearer k"


@pytest.mark.parametrize("answer", [401, 404, 500, "ConnectError", "missing_token"])
def test_a_linked_account_deletion_that_fails_is_failed_and_never_raises(answer, caplog):
    import httpx

    from app.services.erp_oauth import REVOKE_FAILED

    config = dict(_ADAPTER)
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        if answer == "ConnectError":
            client.post = AsyncMock(side_effect=httpx.ConnectError("t"))
        else:
            client.post = AsyncMock(return_value=_resp(answer if isinstance(answer, int) else 200))
        if answer == "missing_token":
            del config["account_token"]
        outcome = _run(MergeDevAdapter(config).revoke_stored_access())
    assert outcome == REVOKE_FAILED
    assert "tok" not in caplog.text and "Bearer" not in caplog.text


def test_the_settle_margin_is_minutes_not_seconds():
    """Guard against the margin being trimmed back to nothing: our client's own
    timeouts alone can run past a minute (httpx's 30 s is per operation)."""
    from datetime import timedelta

    from app.services.erp_adapters.merge_dev import SYNC_SETTLE_MARGIN

    assert SYNC_SETTLE_MARGIN >= timedelta(minutes=5)


# ---------------------------------------------------------------------------
# the change feed and the payment → bill read (decisions §504; Merge's
# Accounting OpenAPI spec read 2026-11-24)
# ---------------------------------------------------------------------------


def _page(results: list[dict], next_cursor: str | None = None) -> MagicMock:
    return _resp(200, {"next": next_cursor, "previous": None, "results": results})


def _routed(pages: dict[str, list[MagicMock]]):
    """A ``client.get`` answering each collection's pages in order."""
    calls: list[tuple[str, dict]] = []

    async def get(url, params=None, headers=None):
        path = url.rsplit("/", 1)[-1]
        calls.append((path, dict(params or {})))
        return pages[path].pop(0)

    return get, calls


def _pay(pid: str, *lines, type_: str | None = "ACCOUNTS_PAYABLE") -> dict:
    return {"id": pid, "type": type_, "applied_to_lines": list(lines)}


def _line(bill: str, kind: str = "INVOICE") -> dict:
    return {"related_object_id": bill, "related_object_type": kind, "applied_amount": "10.00"}


def test_changed_bills_reads_both_feeds_across_cursors_and_dedupes():
    since = datetime(2026, 11, 20, 9, 30, tzinfo=UTC)
    get, calls = _routed(
        {
            "invoices": [
                _page([{"id": "b1", "type": "ACCOUNTS_PAYABLE"}], next_cursor="c2"),
                _page(
                    [
                        {"id": "b2", "type": "ACCOUNTS_PAYABLE", "remote_was_deleted": True},
                        # Merge's filter let a receivable through: not a bill.
                        {"id": "ar1", "type": "ACCOUNTS_RECEIVABLE"},
                    ]
                ),
            ],
            "payments": [
                _page(
                    [
                        _pay("p1", _line("b1"), _line("b3")),
                        _pay("p2", _line("cn1", "CREDIT_NOTE"), _line("je1", "JOURNAL_ENTRY")),
                        _pay("p3", _line("ar2"), type_="ACCOUNTS_RECEIVABLE"),
                        # An unexpanded line is a bare id: nothing we can type.
                        _pay("p4", "line-id-only"),
                    ]
                )
            ],
        }
    )
    with patch("httpx.AsyncClient") as cm:
        cm.return_value.__aenter__.return_value.get = get
        bills = _run(MergeDevAdapter(_ADAPTER).changed_bills(since))
    assert bills == ["b1", "b2", "b3"]
    first_invoices, second_invoices, payments = calls
    assert first_invoices[1] == {
        "modified_after": "2026-11-20T09:30:00Z",
        "include_deleted_data": "true",
        "page_size": "100",
        "type": "ACCOUNTS_PAYABLE",
    }
    assert second_invoices[1]["cursor"] == "c2"
    assert payments[0] == "payments"
    assert payments[1]["expand"] == "applied_to_lines"
    assert payments[1]["modified_after"] == "2026-11-20T09:30:00Z"


def test_changed_bills_states_since_in_utc():
    from datetime import timedelta, timezone

    since = datetime(2026, 11, 20, 11, 30, tzinfo=timezone(timedelta(hours=2)))
    get, calls = _routed({"invoices": [_page([])], "payments": [_page([])]})
    with patch("httpx.AsyncClient") as cm:
        cm.return_value.__aenter__.return_value.get = get
        assert _run(MergeDevAdapter(_ADAPTER).changed_bills(since)) == []
    assert {c[1]["modified_after"] for c in calls} == {"2026-11-20T09:30:00Z"}


@pytest.mark.parametrize("failing", ["invoices", "payments"])
@pytest.mark.parametrize("status", [401, 429, 500])
def test_changed_bills_never_returns_part_of_a_listing(failing, status):
    from app.services.erp_adapters.base import ErpListingIncomplete

    pages = {
        "invoices": [_page([{"id": "b1", "type": "ACCOUNTS_PAYABLE"}], next_cursor="c2")],
        "payments": [_page([_pay("p1", _line("b2"))], next_cursor="c2")],
    }
    pages[failing].append(_resp(status))
    if failing == "payments":
        pages["invoices"].append(_page([]))
    get, _ = _routed(pages)
    with patch("httpx.AsyncClient") as cm:
        cm.return_value.__aenter__.return_value.get = get
        with pytest.raises(ErpListingIncomplete):
            _run(MergeDevAdapter(_ADAPTER).changed_bills(datetime(2026, 11, 1, tzinfo=UTC)))


def test_changed_bills_past_the_row_cap_is_truncated_not_partial():
    from app.services.erp_adapters.base import ErpListingIncomplete, ListingGap

    async def get(url, params=None, headers=None):
        return _page([{"id": "b", "type": "ACCOUNTS_PAYABLE"}], next_cursor="more")

    with patch("httpx.AsyncClient") as cm:
        cm.return_value.__aenter__.return_value.get = get
        with pytest.raises(ErpListingIncomplete) as exc:
            _run(MergeDevAdapter(_ADAPTER).changed_bills(datetime(2026, 11, 1, tzinfo=UTC)))
    assert exc.value.gap is ListingGap.truncated


def test_bills_for_payment_reads_the_payments_invoice_lines():
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(
            return_value=_resp(
                200, _pay("p1", _line("b1"), _line("b1"), _line("cn", "CREDIT_NOTE"))
            )
        )
        assert _run(MergeDevAdapter(_ADAPTER).bills_for_payment("p1")) == ["b1"]
    assert client.get.await_args.args[0].endswith("/payments/p1")
    assert client.get.await_args.kwargs["params"] == {"expand": "applied_to_lines"}


@pytest.mark.parametrize(
    "answer", [_resp(404), _resp(500), _resp(200, {"id": "other", "applied_to_lines": []})]
)
def test_bills_for_payment_never_reads_an_unreadable_payment_as_applied_to_none(answer):
    with patch("httpx.AsyncClient") as cm:
        cm.return_value.__aenter__.return_value.get = AsyncMock(return_value=answer)
        with pytest.raises(RuntimeError):
            _run(MergeDevAdapter(_ADAPTER).bills_for_payment("p1"))


def test_merge_takes_no_payment_write_back_so_its_pushes_close_at_done():
    """Merge's Payment has no memo / reference field and no DELETE (decisions
    §504): a write-back could not be found again before a retry, told apart
    from a person's payment, or reversed. Until that changes, Merge is not a
    status-sync adapter, so a push is never left payable here."""
    from app.services.erp_adapters.base import PaymentPayload
    from app.services.erp_adapters.dispatcher import status_sync_adapter_keys

    adapter = MergeDevAdapter(_ADAPTER)
    assert adapter.supports_status_sync is False
    assert "merge_dev" not in status_sync_adapter_keys()
    payload = PaymentPayload(
        payment_id="pay-1",
        bill_id="b1",
        amount=Decimal("10.00"),
        currency="USD",
        paid_on=date(2026, 11, 20),
        pay_from_account_erp_id="acct",
    )
    with patch("httpx.AsyncClient") as cm:
        for write in (adapter.post_payment, adapter.void_payment):
            result = _run(write(payload))
            assert result.success is False and result.retryable is False
    cm.assert_not_called()
