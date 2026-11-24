import hashlib
import json
from decimal import Decimal
from pathlib import Path

from app.catalog import SPEC
from app.domain.calculate import calculate_rows
from app.domain.decision import decide_rows
from app.domain.prepare import prepare_rows
from app.reconcile.ledger import build_ledger


def jsonable(value):
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items() if not str(key).startswith("_")}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


def _stage(name):
    return SPEC["stages"].index(name) + 1


TRACE_KEYS = {
    "shipment_id", "shipment_ids", "promise_source", "commitment_source", "deadline_source",
    "promise_origin", "promise_kind", "promise_waybill_id", "commitment_waybill_id",
    "source_waybill_id", "promise_id", "commitment_id", "promise_deadline_at",
    "commitment_deadline_at", "effective_deadline_at", "original_deadline_at",
    "new_deadline_at", "used_original_promise", "used_new_promise",
    "hold", "on_hold", "issuable", "can_issue", "conflict", "has_conflict",
    "evidence_conflict", "switch_conflict", "payout_affecting", "payout_changes",
    "conflict_code", "original_compensation_cents", "new_compensation_cents",
    "payout_cents", "final_delivered_at", "signed_at", "status", "state",
    "label", "verdict", "reason", "metric", "limit", "breakdown",
}


def _intake_from_rows(rows, profile="adhoc"):
    """让 HTTP/调用方直接提交 dict 行；保留换单等扩展键。

    对缺失的数值列补 0（与测试 ``row()`` 助手的完整行习惯一致），日期/主键
    缺失仍交由 prepare 严格隔离。
    """
    numeric_kinds = {"int", "int_positive", "money"}
    normalized = []
    for index, raw in enumerate(rows or [], start=2):
        item = dict(raw or {})
        item["_line"] = raw.get("_line", index) if isinstance(raw, dict) else index
        for column in SPEC["columns"]:
            if item.get(column) is None:
                kind = SPEC["types"].get(column, "")
                item[column] = 0 if kind in numeric_kinds else ""
            elif column in item and item[column] is not None and not isinstance(
                    item[column], (bool, int, float, Decimal)):
                item[column] = str(item[column])
        normalized.append(item)
    digest = hashlib.sha256(
        json.dumps(normalized, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    counts = {"input": len(normalized), "accepted": len(normalized), "isolated": 0,
              "duplicates": 0, "ignored": 0}
    return {"profile": profile, "path": Path("api-rows.json"), "sha256": digest,
            "rows": normalized, "issues": [], "counts": counts}


def pipeline(profile="dirty", path=None, upto="证据交付", rows=None, records=None,
             csv=None, content=None, text=None, **extra):
    # 兼容 pipeline([...rows...]) 的直接调用
    if isinstance(profile, list):
        rows = profile
        profile = extra.get("profile", "adhoc")
    elif isinstance(profile, dict):
        rows = [profile]
        profile = "adhoc"

    if rows is not None or records is not None:
        intake = _intake_from_rows(rows if rows is not None else records,
                                   profile if isinstance(profile, str) else "adhoc")
        profile = intake["profile"]
    elif csv is not None or content is not None or text is not None:
        from app.intake.service import load_csv_text
        intake = load_csv_text(csv if csv is not None else (content if content is not None else text),
                               profile=profile if isinstance(profile, str) else "adhoc")
    else:
        from app.intake.service import load
        if not isinstance(profile, str):
            profile = "dirty"
        intake = load(profile, path)

    prepared, prepare_issues = [], []
    results = []
    ledger = None
    if _stage(upto) >= 2:
        prepared, prepare_issues = prepare_rows(intake["rows"])
    if _stage(upto) >= 3:
        results = calculate_rows(prepared)
    if _stage(upto) >= 4:
        results = decide_rows(results)
    if _stage(upto) >= 5:
        ledger = build_ledger(intake, prepared, prepare_issues, results)
    intake["counts"]["prepared"] = len(prepared)
    intake["counts"]["computed"] = len(results)
    issues = intake["issues"] + prepare_issues
    summary = {"PASS": 0, "REVIEW": 0, "REJECT": 0}
    for item in results:
        verdict = item.get("verdict", "REVIEW")
        key = str(verdict)
        summary[key] = summary.get(key, 0) + 1
    chart_field = {"0840": "rolling", "0849": "mean", "0850": "settlement_cents"}.get(SPEC["code"], "metric")
    chart = [{"label": item.get("label", item.get(SPEC["key"], "")),
              "value": float(item.get(chart_field) or 0),
              "limit": float(item.get("limit") or 0)}
             for item in results[:12]]
    rows_out = []
    raw_stage = _stage(upto) <= 2
    if raw_stage:
        stage_rows = intake["rows"] if _stage(upto) == 1 else prepared
        rows_out = [{key: item.get(key) for key in SPEC["columns"]} for item in stage_rows[:18]]
    else:
        for item in results[:18]:
            rows_out.append({
                key: value for key, value in item.items()
                if not str(key).startswith("_") and not callable(value)
            })
    source = {"profile": profile, "filename": Path(str(intake["path"])).name, "sha256": intake["sha256"]}

    hold_count = sum(1 for item in results if item.get("hold") or item.get("on_hold"))
    blocked = summary.get("REJECT", 0) > 0 or hold_count > 0 or bool(ledger and ledger.get("holds"))
    run_seed = SPEC["code"] + str(profile) + intake["sha256"] + SPEC["rule"]
    payload = {
        "project": SPEC["code"] + " " + SPEC["name"],
        "rule": SPEC["rule_short"],
        "profile": profile,
        "source": source,
        "counts": intake["counts"],
        "issues": issues[:60],
        "rows": rows_out,
        "row_kind": "source" if raw_stage else "result",
        "chart": chart,
        "summary": summary,
        "ledger": ledger,
        "hold_count": hold_count,
        "gate": {"blocked": blocked, "approval_allowed": not blocked,
                 "reject_count": summary.get("REJECT", 0), "hold_count": hold_count},
        "stages": [{"id": "intake", "name": SPEC["stages"][0]},
                   {"id": "prepare", "name": SPEC["stages"][1]},
                   {"id": "calculate", "name": SPEC["stages"][2]},
                   {"id": "decision", "name": SPEC["stages"][3]},
                   {"id": "reconcile", "name": SPEC["stages"][4]}],
        "run_key": hashlib.sha256(run_seed.encode("utf-8")).hexdigest()[:24],
    }
    if ledger is not None:
        payload["shipments"] = ledger.get("shipments")
        payload["payouts"] = ledger.get("payouts")
    return jsonable(payload)
