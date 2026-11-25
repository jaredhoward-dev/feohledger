"""The gates for the contract lifecycle actions — activate / terminate / cancel /
renew, create a PO from a contract, and delete. Each is the ONE definition its
endpoint raises from and the row's ``actions`` verdict is read from (issue #575
§ One rule, one place; ``docs/decisions.md`` §507), so the /contracts list and
``ContractModal`` stop restating the status lists. The page's old copy had
already drifted: it hid Activate on an ``expired`` contract and Terminate on an
``expired`` one, both of which the endpoints accept, and hid Create PO on a
``draft`` the endpoint accepts.

Every gate returns an :class:`~app.api.refusals.ActionRefusal` or ``None``. The
endpoint raises ``refusal.http()``; every gate here is ``coded=True``, so the
409 ``detail`` is the coded ``{code, message, params}`` the verdict carries,
whose English ``message`` is the sentence the endpoint always answered with.
``api/contracts.contract_action_refusals`` ships ``refusal.verdict()`` on the
row after the endpoints' own role predicate.
"""

from __future__ import annotations

from typing import Literal

from fastapi import status

from app.api.refusals import ActionRefusal
from app.models.contract import Contract, ContractStatus

#: A lifecycle action (activate / terminate / cancel / renew / create_po) the
#: contract's current status does not allow. ``params``: ``status`` (the
#: contract's), ``action``. 409.
CONTRACT_STATUS_REFUSED = "contract_status_refused"
#: Delete of a contract that is neither ``draft`` nor ``cancelled``: an active
#: (or once-active) contract is part of the spend story, so it is terminated or
#: cancelled instead. ``params``: ``status``. 409.
CONTRACT_DELETE_REFUSED = "contract_delete_refused"

ContractTransition = Literal["activate", "terminate", "cancel"]

#: The statuses each one-step transition starts from. draft→active (sign),
#: expired→active (re-activate); active/expired→terminated; draft/active→
#: cancelled. Renew and create-PO have their own gates below.
TRANSITION_FROM: dict[str, frozenset[ContractStatus]] = {
    "activate": frozenset({ContractStatus.draft, ContractStatus.expired}),
    "terminate": frozenset({ContractStatus.active, ContractStatus.expired}),
    "cancel": frozenset({ContractStatus.draft, ContractStatus.active}),
}
#: Where each transition lands.
TRANSITION_TARGET: dict[str, ContractStatus] = {
    "activate": ContractStatus.active,
    "terminate": ContractStatus.terminated,
    "cancel": ContractStatus.cancelled,
}
#: Renew refuses only the two terminal statuses (it re-activates an expired
#: contract and pushes a draft's or active one's end date).
RENEW_REFUSED_FROM = frozenset({ContractStatus.terminated, ContractStatus.cancelled})
#: Only a draft or active contract spawns a PO.
CREATE_PO_FROM = frozenset({ContractStatus.draft, ContractStatus.active})
#: Only a draft or cancelled contract deletes.
DELETE_FROM = frozenset({ContractStatus.draft, ContractStatus.cancelled})


def _status_refusal(contract: Contract, action: str, message: str) -> ActionRefusal:
    return ActionRefusal(
        CONTRACT_STATUS_REFUSED,
        message,
        status.HTTP_409_CONFLICT,
        params={"status": str(contract.status), "action": action},
        coded=True,
    )


def contract_transition_refusal(
    contract: Contract, action: ContractTransition
) -> ActionRefusal | None:
    """Activate / terminate / cancel: the contract's status must be one the
    action starts from (:data:`TRANSITION_FROM`)."""
    if contract.status in TRANSITION_FROM[action]:
        return None
    return _status_refusal(
        contract, action, f"Cannot {action} a contract in '{contract.status}' status"
    )


def contract_renew_refusal(contract: Contract) -> ActionRefusal | None:
    """Renew: refused on a terminated or cancelled contract. (The new end date
    being after the current one is a check on the request body, a 400, and so
    not part of the row's verdict.)"""
    if contract.status not in RENEW_REFUSED_FROM:
        return None
    return _status_refusal(
        contract, "renew", f"Cannot renew a contract in '{contract.status}' status"
    )


def contract_create_po_refusal(contract: Contract) -> ActionRefusal | None:
    """Create PO: only a draft or active contract."""
    if contract.status in CREATE_PO_FROM:
        return None
    return _status_refusal(
        contract,
        "create_po",
        f"Cannot create a PO from a contract in '{contract.status}' status",
    )


def contract_delete_refusal(contract: Contract) -> ActionRefusal | None:
    """Delete: only a draft or cancelled contract."""
    if contract.status in DELETE_FROM:
        return None
    return ActionRefusal(
        CONTRACT_DELETE_REFUSED,
        f"Cannot delete a contract in '{contract.status}' status — terminate or cancel it instead",
        status.HTTP_409_CONFLICT,
        params={"status": str(contract.status)},
        coded=True,
    )
