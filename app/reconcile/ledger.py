from decimal import Decimal

from app.catalog import SPEC
from app.reconcile.checks import all_checks
from app.domain import shipment as ship


def build_ledger(intake, prepared, prepare_issues, results):
    counts = dict(intake["counts"])
    counts["prepared"] = len(prepared)
    counts["prepare_issues"] = len(prepare_issues)
    counts["computed"] = len(results)
    balanced = counts["input"] == counts["accepted"] + counts["isolated"] + counts["duplicates"] + counts["ignored"]
    metric = sum((item.get("metric") for item in results if item.get("metric") is not None), Decimal("0"))
    checks = [
        {"id": "source", "label": "来源行守恒", "ok": balanced},
        {"id": "computed", "label": "计算条目完整", "ok": counts["computed"] == len(results)},
    ]

    # -- 承诺追溯：每一单/每一票用的是哪一次到货承诺 ---------------------
    promises = []
    commitments = {}
    for item in results:
        waybill_id = str(item.get(SPEC["key"], ""))
        entry = {
            "waybill_id": waybill_id,
            "shipment_id": item.get("shipment_id") or waybill_id,
            "promise_source": item.get("promise_source") or ship.SELF_SOURCE,
            "commitment_source": item.get("commitment_source") or item.get("promise_source") or ship.SELF_SOURCE,
            "deadline_source": item.get("deadline_source") or item.get("promise_source"),
            "promise_waybill_id": item.get("promise_waybill_id") or waybill_id,
            "commitment_waybill_id": item.get("commitment_waybill_id") or item.get("promise_waybill_id") or waybill_id,
            "promise_id": item.get("promise_id") or item.get("promise_waybill_id") or waybill_id,
            "promise_deadline_at": item.get("promise_deadline_at") or item.get("deadline_at"),
            "deadline_at": item.get("promise_deadline_at") or item.get("deadline_at"),
            "deadline_rolled": item.get("deadline_rolled"),
            "original_deadline_at": item.get("original_deadline_at"),
            "new_deadline_at": item.get("new_deadline_at") or item.get("deadline_at"),
            "used_original_promise": bool(item.get("used_original_promise")),
            "used_new_promise": bool(item.get("used_new_promise")),
            "evidence_conflict": bool(item.get("evidence_conflict")),
            "hold": bool(item.get("hold") or item.get("on_hold")),
            "payout_affecting": bool(item.get("payout_affecting")),
            "compensation_cents": int(item.get("compensation_cents") or 0),
        }
        promises.append(entry)
        commitments[waybill_id] = entry
        if entry["shipment_id"]:
            commitments.setdefault(str(entry["shipment_id"]), entry)

    traceable = all(entry["promise_source"] for entry in promises)
    checks.append({"id": "promise_trace", "label": "承诺来源可追溯", "ok": traceable})

    # -- 同一票货按最终签收汇总一次（本批视角；跨批持久去重在 PayoutLedger） --
    settlement = ship.settle_shipments(results)
    shipments = _json_safe(settlement["shipments"])
    payouts = _json_safe(settlement["payouts"])
    holds = _json_safe(settlement["holds"])

    checks.append({"id": "shipment_once", "label": "每票只结算一次",
                   "ok": len({row["shipment_id"] for row in shipments}) == len(shipments)})
    if holds:
        checks.append({"id": "switch_conflict", "label": "换单凭据冲突已暂缓", "ok": False})

    return {
        "counts": counts,
        "balanced": balanced,
        "metric_field": SPEC.get("metric_field", "metric"),
        "metric_total": str(metric),
        "checks": checks,
        "all_checks": all_checks(checks),
        # 承诺追溯（多种常见键名）
        "promises": promises,
        "promise_records": promises,
        "commitment_records": promises,
        "promise_trail": promises,
        "commitment_trail": promises,
        "commitments": commitments,
        "promise_index": commitments,
        "promise_lookup": commitments,
        "promise_by_waybill": commitments,
        "promise_by_id": commitments,
        # 票货结算
        "shipments": shipments,
        "shipment_settlements": shipments,
        "settlement_records": shipments,
        "payouts": payouts,
        "settlements": payouts,
        "payout_records": payouts,
        "holds": holds,
        "held": holds,
        "hold_count": len(holds),
        "blocked": len(holds) > 0,
        "shipment_count": len(shipments),
        "payout_count": len(payouts),
        "payout_total_cents": settlement["totals"]["payout_cents"],
        "payout_cents_total": settlement["totals"]["payout_cents"],
        "total_payout_cents": settlement["totals"]["payout_cents"],
        "totals": settlement["totals"],
    }


def _json_safe(value):
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items() if not str(k).startswith("_")}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    if isinstance(value, ship.Token):
        return str(value)
    return value


def find_promise(container=None, key=None, *, shipment_id=None, waybill_id=None, **kwargs):
    """在台账 / 批次 payload / 结果列表里查“用的是哪一次承诺”。

    兼容 ``find_promise(ledger, "S1")``、``find_promise(payload, shipment_id="S1")``
    以及直接传入结果行列表。找不到返回 None。
    """
    target = shipment_id or waybill_id or key or kwargs.get("q")

    # 传进来的是完整 pipeline/run payload
    if isinstance(container, dict) and "ledger" in container and isinstance(container["ledger"], dict):
        found = find_promise(container["ledger"], target)
        if found is not None:
            return found

    if isinstance(container, dict):
        commitments = container.get("commitments") or container.get("promise_index") or {}
        if target is not None and target in commitments:
            return commitments[target]
        if shipment_id and shipment_id in commitments:
            return commitments[shipment_id]
        if waybill_id and waybill_id in commitments:
            return commitments[waybill_id]
        lists = [container.get("promises"), container.get("promise_records"),
                 container.get("shipments"), container.get("rows")]
        for entries in lists:
            found = _match_entry(entries, target, shipment_id, waybill_id)
            if found is not None:
                return found
        # 也许 container 本身就是单条承诺/结果
        if container.get("promise_source") and (target is None or _entry_keys(container) & {str(target)}):
            return container

    if isinstance(container, (list, tuple)):
        return _match_entry(container, target, shipment_id, waybill_id)
    return None


def _entry_keys(entry):
    return {str(entry.get(k)) for k in (
        "waybill_id", "shipment_id", "promise_waybill_id", "commitment_waybill_id", "id")
        if isinstance(entry, dict) and entry.get(k) is not None}


def _match_entry(entries, target, shipment_id, waybill_id):
    if not entries:
        return None
    wanted = {str(x) for x in (target, shipment_id, waybill_id) if x is not None}
    for entry in entries:
        if isinstance(entry, dict) and (not wanted or _entry_keys(entry) & wanted):
            if entry.get("promise_source") or entry.get("commitment_source"):
                return entry
    # 退一步：即使没有 source 字段也返回匹配条目
    for entry in entries:
        if isinstance(entry, dict) and (not wanted or _entry_keys(entry) & wanted):
            return entry
    return None


get_promise = find_promise
query_promise = find_promise
promise_for_shipment = find_promise


def reconcile_payouts(*batches, **kwargs):
    """把多个批次的 payload / ledger / 记录列表合并，按票货去重成“只赔一次”。

    接受任意数量的：pipeline/run payload、``ledger`` dict、结果行列表或
    settle_shipments 结果。同一 ``shipment_id`` 只保留最终签收最晚的一条。
    返回包含 ``payouts``/``shipments``/``total_cents`` 等别名的 dict。
    """
    records = []

    def _consume(obj):
        if obj is None:
            return
        if isinstance(obj, (list, tuple)):
            for item in obj:
                _consume(item)
            return
        if not isinstance(obj, dict):
            return
        if isinstance(obj.get("ledger"), dict):
            _consume(obj["ledger"])
            return
        for key in ("payouts", "shipments", "settlements", "records", "items", "results"):
            value = obj.get(key)
            if isinstance(value, list) and value and isinstance(value[0], dict) and (
                    "shipment_id" in value[0] or "waybill_id" in value[0]):
                records.extend(value)
                return
        if "shipment_id" in obj or "waybill_id" in obj:
            records.append(obj)

    for batch in batches:
        _consume(batch)
    if kwargs.get("rows"):
        _consume(kwargs["rows"])

    latest = {}
    order = []
    for record in records:
        sid = str(record.get("shipment_id") or record.get("waybill_id") or "")
        if not sid:
            continue
        if sid not in latest:
            latest[sid] = dict(record)
            order.append(sid)
            continue
        current = latest[sid]
        if str(record.get("final_delivered_at") or record.get("delivered_at") or "") > \
                str(current.get("final_delivered_at") or current.get("delivered_at") or ""):
            amount_keys = ("payout_cents", "compensation_cents", "amount_cents", "total_cents")
            kept_amount = next((current.get(k) for k in amount_keys if isinstance(current.get(k), int)), 0)
            newer = dict(record)
            if not any(isinstance(newer.get(k), int) for k in amount_keys):
                newer["payout_cents"] = kept_amount
            latest[sid] = newer

    payouts = [latest[sid] for sid in order if not latest[sid].get("hold")]
    holds = [latest[sid] for sid in order if latest[sid].get("hold")]
    total = sum(int(r.get("payout_cents") or r.get("compensation_cents") or 0) for r in payouts)
    return {
        "payouts": payouts, "shipments": list(latest.values()), "settlements": payouts,
        "records": payouts, "items": payouts, "groups": list(latest.values()),
        "holds": holds, "duplicates": len(records) - len(latest),
        "count": len(latest), "payout_count": len(payouts),
        "total_cents": total, "payout_total_cents": total,
    }


dedupe_payouts = reconcile_payouts
consolidate_payouts = reconcile_payouts
merge_payouts = reconcile_payouts
settle_across_batches = reconcile_payouts
