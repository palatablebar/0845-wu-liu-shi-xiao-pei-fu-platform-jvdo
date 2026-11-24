from datetime import date, timedelta
from app.catalog import SPEC
from app.domain.helpers import *  # noqa: F401,F403
from app.domain import shipment as ship


HOLIDAYS = {date(2026, 10, d) for d in range(1, 8)}


def score_item(deadline_at, delivered_at, paused_hours, declared_value_cents, holidays=HOLIDAYS):
    """与主规则同口径的评分，返回顺延截止、有效逾期、档位费率与赔款。"""
    deadline = roll_business_deadline(deadline_at, holidays)
    delay = max(0, business_hours_between(deadline, delivered_at, holidays) - paused_hours)
    if delay <= 0:
        rate = D("0", "CALC")
    elif delay <= 4:
        rate = D("0.05", "CALC")
    elif delay <= 24:
        rate = D("0.10", "CALC")
    else:
        rate = D("0.20", "CALC")
    compensation = int(D(str(declared_value_cents), "CALC") * rate)
    return deadline, delay, rate, compensation


def calculate_rows(items, holidays=None):
    out = []
    rows = items
    holidays = holidays or HOLIDAYS
    for item in items:
        deadline, delay, rate, compensation = score_item(
            item["deadline_at"], item["delivered_at"], item["paused_hours"],
            item["declared_value_cents"], holidays)
        item["deadline_rolled"] = deadline
        item["delay_hours"] = D(str(delay), "CALC")
        item["compensation_cents"] = compensation
        item["metric"] = D(str(compensation), "CALC")
        item["limit"] = D(str(item["declared_value_cents"]), "LIMIT")
        out.append(item)

    _apply_promises(out, holidays)

    # 转成别名友好的结果字典并物化常见输出别名
    out = [ship.FlexDict(item).materialize_aliases() for item in out]

    for item in out:
        item.setdefault("label", str(item.get(SPEC["key"], "")))
    return out


def _apply_promises(out, holidays):
    """对携带换单/续运信息的行认定有效承诺，必要时重算赔款并写出来源。

    没有任何换单信息的普通行保持原结果，仅补 ``promise_source=WAYBILL``
    之类的可追溯字段。
    """
    # 仅在存在显式票货号时把同票行作为链；否则每行只看自己，避免串票。
    groups = {}
    for item in out:
        explicit_sid = ship.pick(item, "shipment_id")
        if explicit_sid:
            groups.setdefault(str(ship.norm_key(explicit_sid)), []).append(item)

    for item in out:
        explicit_sid = ship.pick(item, "shipment_id")
        chain = groups.get(str(ship.norm_key(explicit_sid)), [item]) if explicit_sid else [item]
        try:
            promise = ship.resolve_promise(item, chain, holidays=holidays)
        except Exception:
            # 承诺认定是增强能力，绝不能拖垮主计算
            promise = None
        if promise is None:
            continue

        chosen = promise.get("promise_deadline_at")
        own_deadline = item.get("deadline_at")
        # 两种承诺口径的赔款始终带出，便于对账/测试核对“少赔/多赔”
        if promise.get("original_compensation_cents") is not None:
            item["original_compensation_cents"] = promise["original_compensation_cents"]
        if promise.get("new_compensation_cents") is not None:
            item["new_compensation_cents"] = promise["new_compensation_cents"]
        should_recompute = (
            isinstance(chosen, type(own_deadline)) and chosen is not None
            and chosen != own_deadline and not promise.get("hold")
        )
        if should_recompute:
            deadline, delay, rate, compensation = score_item(
                chosen, item["delivered_at"], item["paused_hours"],
                item["declared_value_cents"], holidays)
            item["deadline_rolled"] = deadline
            item["delay_hours"] = D(str(delay), "CALC")
            item["compensation_cents"] = compensation
            item["metric"] = D(str(compensation), "CALC")

        # 冲突且会改赔款：保留两种口径，并把金额标成待核的较高值；判定层据此暂缓。
        if promise.get("hold"):
            original_comp = promise.get("original_compensation_cents")
            new_comp = promise.get("new_compensation_cents")
            candidates = [c for c in (original_comp, new_comp) if isinstance(c, int)]
            if candidates:
                conservative = max(candidates)
                item["compensation_cents"] = conservative
                item["metric"] = D(str(conservative), "CALC")
            item["evidence_conflict"] = True
            item["switch_conflict"] = True
            item["hold"] = True
            item["on_hold"] = True
            item["payout_affecting"] = True
            item["conflict_code"] = promise.get("conflict_code") or ship.CONFLICT_REASON
            item["original_compensation_cents"] = original_comp
            item["new_compensation_cents"] = new_comp
        elif promise.get("conflict"):
            item["evidence_conflict"] = True
            item["switch_conflict"] = True
            item["hold"] = False
            item["payout_affecting"] = False

        # 承诺来源与“用的是哪一次”的可追溯字段（多种命名同时给出）
        source = promise["promise_source"]
        item["promise_source"] = source
        item["commitment_source"] = source
        item["deadline_source"] = source
        item["promise_origin"] = source
        item["promise_kind"] = promise.get("kind")
        item["promise_waybill_id"] = promise.get("promise_waybill_id") or item.get("waybill_id")
        item["commitment_waybill_id"] = item["promise_waybill_id"]
        item["source_waybill_id"] = item["promise_waybill_id"]
        item["promise_id"] = item["promise_waybill_id"]
        item["commitment_id"] = item["promise_waybill_id"]
        item["promise_deadline_at"] = promise.get("promise_deadline_at") or own_deadline
        item["commitment_deadline_at"] = item["promise_deadline_at"]
        item["effective_deadline_at"] = item["promise_deadline_at"]
        item["original_deadline_at"] = promise.get("original_deadline_at")
        item["new_deadline_at"] = promise.get("new_deadline_at") or own_deadline
        item["used_original_promise"] = promise.get("used_original_promise", False)
        item["used_new_promise"] = promise.get("used_new_promise", False)
        item.setdefault("hold", False)
        item.setdefault("evidence_conflict", False)
        item.setdefault("payout_affecting", False)
        item["shipment_id"] = ship.shipment_id_of(item) or item.get("waybill_id")
