"""The AP-reply email's portal link lands on a page the portal serves.

Kept out of ``test_supplier_chat.py``, whose module-wide ``realdb`` fixture
skips every test without a live Postgres: this check needs none."""

from __future__ import annotations

import re
import uuid
from pathlib import Path


def test_portal_chat_link_opens_a_real_portal_page():
    """The AP-reply email's link must land on a page the portal serves.

    It used to point at ``/portal/invoices/<id>/chat``, which no portal route
    renders, so every supplier who followed it hit a 404. The path must be a
    real SvelteKit route, and the query parameter must be the one that page
    reads to open the conversation."""
    from urllib.parse import parse_qs, urlsplit

    from app.services.supplier_chat import PORTAL_CHAT_PARAM, portal_chat_path

    invoice_id = uuid.uuid4()
    parts = urlsplit(portal_chat_path(invoice_id))
    assert parts.path == "/portal/invoices"
    assert parse_qs(parts.query) == {PORTAL_CHAT_PARAM: [str(invoice_id)]}

    page = (
        Path(__file__).resolve().parents[2]
        / "frontend/src/routes"
        / parts.path.lstrip("/")
        / "+page.svelte"
    )
    assert page.is_file(), f"no portal page at {parts.path}"
    declared = re.search(r"const CHAT_LINK_PARAM = '([^']+)';", page.read_text())
    assert declared, "the portal invoice list no longer declares CHAT_LINK_PARAM"
    assert declared.group(1) == PORTAL_CHAT_PARAM
