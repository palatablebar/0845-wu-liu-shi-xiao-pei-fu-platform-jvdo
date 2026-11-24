import json

from app.errors import DomainIssue
from app.review.policy import approval_allowed


def create_run(store, payload):
    row = store.create(payload["run_key"], payload["profile"], payload)
    return serialize(row)


def approve(store, run_id, decision, comment, expected_revision):
    row = store.get(run_id)
    if row is None:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    payload = json.loads(row["payload"])
    if decision == "APPROVED" and not approval_allowed(payload):
        raise DomainIssue("GATE_BLOCKED", 409)
    normalized = _normalize_decision(decision)
    if normalized is None:
        raise DomainIssue("BAD_REQUEST")
    row, problem = store.review(run_id, normalized, comment.strip(), expected_revision)
    if problem == "missing":
        raise DomainIssue("RUN_NOT_FOUND", 404)
    if problem == "stale":
        raise DomainIssue("STALE_REVIEW", 409)

    result = serialize(row)
    if normalized == "APPROVED":
        # 签发即入账：按票货全局去重，重试/跨批都不会重复给付。
        try:
            receipt = record_issuance_payouts(store, result)
            if receipt is not None:
                result["payout_receipt"] = receipt
                result["payouts"] = store.list_payouts(run_id=result["run_id"]) if hasattr(store, "list_payouts") else []
        except Exception:
            # 入账异常不应让签发结果丢失；但台账仍可由对账接口重建。
            result["payout_receipt"] = {"recorded": [], "duplicates": [], "warning": "LEDGER_WRITE_FAILED"}
    return result


_HOLD_DECISIONS = {"HELD", "HOLD", "SUSPEND", "SUSPENDED", "PENDING", "DEFER", "DEFERRED", "PAUSE"}
_REJECT_DECISIONS = {"REJECT", "REJECTED", "BLOCK", "BLOCKED", "DENY", "DENIED"}


def _normalize_decision(decision):
    if not isinstance(decision, str):
        return None
    token = decision.strip().upper()
    if token == "APPROVED" or token == "APPROVE" or token == "PASS" or token == "ISSUE":
        return "APPROVED"
    if token in _HOLD_DECISIONS:
        return "HELD"
    if token in _REJECT_DECISIONS:
        return "REJECT"
    return None


def record_issuance_payouts(store, run_payload):
    """把一次已签发批次的应付写入持久票货台账（幂等）。"""
    if not hasattr(store, "record_batch"):
        return None
    ledger = run_payload.get("ledger") or {}
    payouts = ledger.get("payouts")
    if payouts is None:
        settlements = ledger.get("settlements") or run_payload.get("settlements") or []
        payouts = [item for item in settlements if not item.get("hold")]
    batch_key = run_payload.get("run_key") or run_payload.get("run_id")
    return store.record_batch(
        payouts,
        batch_key=batch_key,
        run_id=run_payload.get("run_id"),
    )


def serialize(row):
    if row is None:
        return None
    payload = json.loads(row["payload"])
    payload.update(run_id=row["id"], status=row["status"], revision=row["revision"],
                   comment=row["comment"], created_at=row["created_at"])
    return payload
