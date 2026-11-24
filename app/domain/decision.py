from app.domain.helpers import D


def decide_rows(items):
    for item in items:
        if item["invoice_cents"] > item["declared_value_cents"]:
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
        item.setdefault("verdict", "PASS")
        item.setdefault("reason", "OK")
    return items
