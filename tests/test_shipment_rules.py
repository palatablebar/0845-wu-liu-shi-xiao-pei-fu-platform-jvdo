"""换单续运 / 取消重发 / 冲突暂缓 / 跨批一次赔 / 承诺可追溯的行为固定。

这些用例按业务描述逐句构造，既验证领域函数，也验证 HTTP 闸门与持久台账。
"""

import os

import pytest

from app.domain.shipment import (
    PayoutLedger,
    resolve_promise,
    settle_shipments,
)
from tests.domain_cases import row, run


def _switch_row(**changes):
    item = row(
        deadline_at="2026-11-09 18:00",      # 新单承诺（较晚）
        delivered_at="2026-11-09 16:00",
        paused_hours="0",
        declared_value_cents="10000",
        invoice_cents="500",
        original_deadline_at="2026-11-09 12:00",  # 原承诺（较早）
        shipment_id="SH-1",
    )
    item.update(changes)
    return item


def test_continuation_honors_original_promise():
    """旧单已揽收 + 续运凭据 -> 认原承诺，赔 500 而不是 0。"""
    item = _switch_row(picked_up="true", continuation_evidence="XY-001")
    results, issues = run([item])
    assert issues == []
    out = results[0]
    assert out["compensation_cents"] == 500
    assert out["promise_source"] == "ORIGINAL"
    assert out["used_original_promise"] is True


def test_only_new_deadline_would_underpay():
    """对照：只认新单 18:00 截止，16:00 签收不逾期 -> 0（少赔）。"""
    results, _ = run([row(deadline_at="2026-11-09 18:00",
                          delivered_at="2026-11-09 16:00", paused_hours="0",
                          declared_value_cents="10000")])
    assert results[0]["compensation_cents"] == 0
    continued, _ = run([_switch_row(picked_up="1", continuation_evidence="E")])
    assert continued[0]["compensation_cents"] > results[0]["compensation_cents"]


def test_cancel_before_pickup_reship_uses_new_promise():
    """旧单未揽收即取消、重新发货 -> 认新承诺，赔 0。"""
    item = _switch_row(picked_up="false", cancelled="true", reship="true",
                       invoice_cents="0", shipment_id="SH-2")
    out = run([item])[0][0]
    assert out["compensation_cents"] == 0
    assert out["promise_source"] == "NEW"


def test_keeping_old_promise_would_overpay():
    """对照：沿用旧的 12:00 承诺会算出 500（多赔），正确口径是 0。"""
    item = _switch_row(picked_up="false", cancelled="true", reship="true",
                       invoice_cents="0", shipment_id="SH-3")
    out = run([item])[0][0]
    assert out["original_compensation_cents"] == 500
    assert out["new_compensation_cents"] == 0
    assert out["compensation_cents"] == 0


def test_conflicting_evidence_that_changes_payout_holds():
    item = _switch_row(picked_up="true", continuation_evidence="E-1",
                       cancelled="true", reship="true", evidence_conflict="true",
                       invoice_cents="0", shipment_id="SH-C")
    out = run([item])[0][0]
    assert out["hold"] is True
    assert out["payout_affecting"] is True
    assert out["evidence_conflict"] is True
    # 判定同时兼容 REVIEW/REJECT/HELD 命名，但必须能被闸门拦截
    assert out["verdict"] in {"REVIEW", "REJECT", "HELD", "BLOCKED"}


def test_conflict_without_payout_difference_is_not_gate_blocker():
    """两种承诺赔款相同 -> 虽记录冲突，但不形成赔款意义上的暂缓。"""
    result = resolve_promise(
        deadline_at="2026-11-09 18:00",
        original_deadline_at="2026-11-09 17:00",  # 都在签收前，赔款均为 0
        delivered_at="2026-11-09 16:00",
        paused_hours=0, declared_value_cents=10000,
        picked_up=True, continuation_evidence="E", cancelled=True, reship=True,
        evidence_conflict=True,
    )
    assert result["conflict"] is True
    assert result["payout_affecting"] is False
    assert result["hold"] is False


def test_settle_groups_shipment_once_by_final_delivery():
    groups = settle_shipments([
        {"shipment_id": "S9", "waybill_id": "W-a",
         "delivered_at": "2026-11-09 12:00", "compensation_cents": 500},
        {"shipment_id": "S9", "waybill_id": "W-b",
         "delivered_at": "2026-11-10 18:00", "final_delivered_at": "2026-11-10 18:00",
         "compensation_cents": 900},
    ])
    assert len(groups["shipments"]) == 1
    record = groups["shipments"][0]
    assert record["shipment_id"] == "S9"
    assert str(record["final_delivered_at"]).startswith("2026-11-10")


def test_payout_ledger_cross_batch_and_retry_idempotent(tmp_path):
    ledger = PayoutLedger(tmp_path / "payout.sqlite3")
    first = ledger.record_batch([{"shipment_id": "S1", "payout_cents": 500,
                                  "promise_source": "ORIGINAL"}], batch_key="B1")
    assert first["paid_count"] == 1
    # 同批重试
    retry = ledger.record_batch([{"shipment_id": "S1", "payout_cents": 500}], batch_key="B1")
    assert retry["paid_count"] == 0 and retry["duplicate_count"] == 1
    # 跨批再次出现同一票货
    second_batch = ledger.record_batch([{"shipment_id": "S1", "payout_cents": 500}], batch_key="B2")
    assert second_batch["paid_count"] == 0
    assert ledger.payout_count() == 1
    assert ledger.payout_total_cents() == 500
    promise = ledger.promise_for(shipment_id="S1")
    assert promise["promise_source"] == "ORIGINAL"


@pytest.fixture
def client(tmp_path):
    from app.main import create_app
    app = create_app(str(tmp_path / "wb.sqlite3"))
    app.config.update(TESTING=True)
    return app.test_client()


def test_http_continuation_and_promise_lookup(client):
    payload = {"rows": [_switch_row(picked_up="true", continuation_evidence="E-9")]}
    created = client.post("/api/runs", json=payload).get_json()
    assert created["rows"][0]["compensation_cents"] == 500
    ledger = created["ledger"]
    assert "SH-1" in ledger["commitments"]
    promise = ledger["commitments"]["SH-1"]
    assert promise["promise_source"] == "ORIGINAL"

    approved = client.post(f"/api/runs/{created['run_id']}/review",
                           json={"decision": "APPROVED", "comment": "ok",
                                 "expected_revision": 0})
    assert approved.status_code == 200
    queried = client.get("/api/reconcile/promise?shipment_id=SH-1").get_json()
    assert queried["promise_source"] == "ORIGINAL"
    assert queried["payout_cents"] == 500


def test_http_conflict_blocks_approval(client):
    created = client.post("/api/runs", json={"rows": [
        _switch_row(shipment_id="SH-BLOCK", picked_up="1",
                     continuation_evidence="E", cancelled="1", reship="1",
                     evidence_conflict="1")]}).get_json()
    response = client.post(f"/api/runs/{created['run_id']}/review",
                           json={"decision": "APPROVED", "comment": "x",
                                 "expected_revision": 0})
    assert response.status_code == 409
    assert response.get_json()["code"] == "GATE_BLOCKED"


def test_http_issuance_retry_does_not_double_pay(client):
    created = client.post("/api/runs", json={"rows": [_switch_row(shipment_id="SH-PAY")]}).get_json()
    url = f"/api/runs/{created['run_id']}/review"
    first = client.post(url, json={"decision": "APPROVED", "comment": "1", "expected_revision": 0})
    assert first.status_code == 200
    again = client.post(url, json={"decision": "APPROVED", "comment": "2", "expected_revision": 1})
    assert again.status_code == 200
    payouts = client.get("/api/payouts").get_json()
    sh = [item for item in payouts["items"] if item["shipment_id"] == "SH-PAY"]
    assert len(sh) == 1
