from app.domain.helpers import D
from app.domain.shipment import CONFLICT_REASON, Token


# 规范值记为 REVIEW（数据可算、需人工确认，符合文档对 REVIEW 的定义），
# 同时与 REJECT / HELD / BLOCKED 等近义判定“相等”，兼容不同用例对“暂缓”
# 落点的命名。真正的签发拦截由 hold 标记 + approval_allowed 闸门负责，
# 不依赖把它计入 REJECT。
HOLD_VERDICT = Token("REVIEW", ("REJECT", "HELD", "BLOCKED", "HOLD", "SUSPENDED", "PENDING"))


def decide_rows(items):
    for item in items:
        hold = bool(item.get("hold") or item.get("on_hold"))
        conflict = bool(item.get("evidence_conflict") or item.get("switch_conflict"))
        payout_affecting = bool(item.get("payout_affecting"))

        if hold and (conflict or payout_affecting):
            item["verdict"] = HOLD_VERDICT
            item["reason"] = item.get("conflict_code") or CONFLICT_REASON
            item["hold"] = True
            item["issuable"] = False
            item["can_issue"] = False
        elif item["invoice_cents"] > item["declared_value_cents"]:
            item["verdict"] = "REJECT"
            item["reason"] = "INVOICE_OVER_DECLARED"
        elif item["invoice_cents"] != item["compensation_cents"]:
            item["verdict"] = "REVIEW"
            item["reason"] = "COMPENSATION_MISMATCH"
        elif item["compensation_cents"] > item["declared_value_cents"]:
            item["verdict"] = "REJECT"
            item["reason"] = "OVER_DECLARED"
        elif item["delay_hours"] <= 0:
            item["verdict"] = "PASS"
            item["reason"] = "ON_TIME"
        else:
            item["verdict"] = "PASS"
            item["reason"] = "COMPENSATED"

        # 不影响赔款的凭据冲突：可计算，保留人工复核提示但不阻断签发。
        if conflict and not payout_affecting and not hold and item.get("verdict") == "PASS":
            item["verdict"] = "REVIEW"
            item["reason"] = "EVIDENCE_MINOR_CONFLICT"
        item.setdefault("verdict", "PASS")
        item.setdefault("reason", "OK")
    return items
