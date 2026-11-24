from datetime import date, timedelta
from app.catalog import SPEC
from app.domain.helpers import *  # noqa: F401,F403


def calculate_rows(items):
    out = []
    rows = items
    holidays = {date(2026, 10, d) for d in range(1, 8)}
    for item in items:
        deadline = roll_business_deadline(item["deadline_at"], holidays)
        delay = max(0, business_hours_between(deadline, item["delivered_at"], holidays) - item["paused_hours"])
        if delay <= 0:
            rate = D("0", "CALC")
        elif delay <= 4:
            rate = D("0.05", "CALC")
        elif delay <= 24:
            rate = D("0.10", "CALC")
        else:
            rate = D("0.20", "CALC")
        compensation = int(D(str(item["declared_value_cents"]), "CALC") * rate)
        item["deadline_rolled"] = deadline
        item["delay_hours"] = D(str(delay), "CALC")
        item["compensation_cents"] = compensation
        item["metric"] = D(str(compensation), "CALC")
        item["limit"] = D(str(item["declared_value_cents"]), "LIMIT")
        out.append(item)
    for item in out:
        item.setdefault("label", str(item.get(SPEC["key"], "")))
    return out
