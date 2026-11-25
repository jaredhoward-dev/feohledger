"""Test helpers for the set-password links invites and admin resets email.

`capture_outbox` swaps the dev `console` email adapter's `send` for a list
append (optionally raising), and `link_token` pulls the single-use token out of
a captured message, the way the recipient's click would.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from app.services.legal_acceptance import CURRENT_LEGAL_VERSIONS

#: What a set-password page sends when its required accept box is ticked: an
#: account with no password yet must accept the Terms / Privacy versions in
#: force to redeem its link (decisions §495).
ACCEPT_CURRENT = {
    "accepted_terms_version": CURRENT_LEGAL_VERSIONS["terms"],
    "accepted_privacy_version": CURRENT_LEGAL_VERSIONS["privacy"],
}

_LINK = re.compile(r"https?://\S+/(?:login|portal)/reset-password\?\S+")


def capture_outbox(monkeypatch, *, fail: bool = False) -> list:
    sent: list = []

    async def _send(self, message):  # noqa: ANN001
        if fail:
            raise RuntimeError("smtp unreachable")
        sent.append(message)

    from app.services.email_adapters.console_adapter import ConsoleAdapter

    monkeypatch.setattr(ConsoleAdapter, "send", _send, raising=True)
    return sent


def set_password_link(message) -> str:  # noqa: ANN001
    """The one set-password link in ``message``'s plaintext body."""
    links = _LINK.findall(message.body_text)
    assert len(set(links)) == 1, links
    return links[0]


def link_token(message) -> str:  # noqa: ANN001
    return parse_qs(urlsplit(set_password_link(message)).query)["token"][0]


def link_mode(message) -> str | None:  # noqa: ANN001
    return parse_qs(urlsplit(set_password_link(message)).query).get("mode", [None])[0]
