from flask import Blueprint, abort, current_app, jsonify, request, send_file

from app.catalog import SPEC
from app.config import SAMPLES
from app.errors import DomainIssue
from app.evidence.render import render
from app.review.service import approve, create_run, serialize
from app.service import pipeline


api = Blueprint("api", __name__)
STAGE_INDEX = {"intake": 1, "prepare": 2, "calculate": 3, "decision": 4, "reconcile": 5}


def store():
    from flask import current_app
    return current_app.extensions["runs"]


def _pipeline_kwargs(data):
    data = data or {}
    rows = data.get("rows")
    if rows is None:
        rows = data.get("records") or data.get("items") or data.get("shipments")
    text = data.get("csv") or data.get("content") or data.get("text") or data.get("data_text")
    return {
        "profile": data.get("profile", "dirty"),
        "path": data.get("path"),
        "upto": data.get("upto", "证据交付"),
        "rows": rows,
        "csv": text,
    }


@api.get("/api/catalog")
def catalog():
    return jsonify(code=SPEC["code"], name=SPEC["name"], actor=SPEC["actor"], rule=SPEC["rule"],
                   rule_short=SPEC["rule_short"],
                   stages=[{"id": item, "name": name} for item, name in zip(
                       ["intake", "prepare", "calculate", "decision", "reconcile", "review", "evidence"],
                       SPEC["stages"])],
                   chart=SPEC["chart"], result_columns=SPEC["result_columns"],
                   columns=SPEC["columns"], samples=["dirty", "clean"])


@api.post("/api/stages/<stage_id>")
def stage(stage_id):
    if stage_id not in STAGE_INDEX:
        raise DomainIssue("STAGE_UNKNOWN", 404)
    data = request.get_json(silent=True) or {}
    profile = data.get("profile", "dirty")
    upto = SPEC["stages"][STAGE_INDEX[stage_id] - 1]
    kwargs = _pipeline_kwargs(data)
    kwargs["upto"] = upto
    if data.get("rows") is None and data.get("records") is None and not any(
            data.get(k) for k in ("csv", "content", "text")):
        return jsonify(pipeline(profile, upto=upto))
    return jsonify(pipeline(**kwargs))


@api.post("/api/runs")
def create():
    data = request.get_json(silent=True) or {}
    if any(data.get(k) is not None for k in ("rows", "records", "items", "shipments", "csv", "content", "text")):
        payload = pipeline(**_pipeline_kwargs(data))
    else:
        profile = data.get("profile", "dirty")
        payload = pipeline(profile)
    return jsonify(create_run(store(), payload))


@api.get("/api/runs/<run_id>")
def get_run(run_id):
    run = serialize(store().get(run_id))
    if not run:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    return jsonify(run)


@api.post("/api/runs/<run_id>/review")
def review(run_id):
    data = request.get_json(silent=True) or {}
    if "decision" not in data or "expected_revision" not in data:
        raise DomainIssue("BAD_REQUEST")
    return jsonify(approve(store(), run_id, data["decision"], data.get("comment", ""), int(data["expected_revision"])))


@api.get("/api/runs/<run_id>/evidence")
def evidence(run_id):
    run = serialize(store().get(run_id))
    return jsonify(render(run))


@api.get("/api/runs/<run_id>/reconcile")
def run_reconcile(run_id):
    run = serialize(store().get(run_id))
    if not run:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    ledger = run.get("ledger") or {}
    key = request.args.get("shipment_id") or request.args.get("waybill_id") or request.args.get("key")
    if key:
        found = _find_promise_in_ledger(ledger, key)
        if found is None:
            raise DomainIssue("PROMISE_NOT_FOUND", 404)
        return jsonify(found)
    return jsonify(ledger)


# ---------------------------------------------------------------------------
# 对账查询：用的是哪次承诺 / 票货赔付台账
# ---------------------------------------------------------------------------


def _find_promise_in_ledger(ledger, key):
    commitments = ledger.get("commitments") or ledger.get("promise_index") or {}
    if key in commitments:
        return commitments[key]
    for entry in ledger.get("promises", []) or []:
        if key in {entry.get("waybill_id"), entry.get("shipment_id"),
                   entry.get("promise_waybill_id"), entry.get("commitment_waybill_id")}:
            return entry
    for entry in ledger.get("shipments", []) or []:
        if key in {entry.get("shipment_id"), entry.get("waybill_id"), entry.get("final_waybill_id")}:
            return entry
    return None


def _query_promise():
    shipment_id = request.args.get("shipment_id") or request.args.get("shipment") or request.args.get("id")
    waybill_id = request.args.get("waybill_id") or request.args.get("waybill")
    key = request.args.get("key") or request.args.get("q")
    if key and not shipment_id and not waybill_id:
        # 先按票、再按单尝试
        shipment_id, waybill_id = key, key

    # 1) 已签发持久台账
    persistent = store().promise_for(shipment_id=shipment_id, waybill_id=waybill_id) if hasattr(store(), "promise_for") else None
    if persistent:
        return jsonify(persistent)

    # 2) 扫描已保存批次的对账台账（草稿也能查到）
    for row in store().list_runs(limit=500):
        run = serialize(row)
        ledger = run.get("ledger") or {}
        found = _find_promise_in_ledger(ledger, shipment_id or waybill_id or key)
        if found is not None:
            found = dict(found)
            found.setdefault("run_id", run.get("run_id"))
            return jsonify(found)
    raise DomainIssue("PROMISE_NOT_FOUND", 404)


for path in ("/api/reconcile/promise", "/api/reconcile/promises",
             "/api/reconcile/commitment", "/api/reconcile/commitments",
             "/api/promises/which", "/api/promise", "/api/promises",
             "/api/commitments"):
    api.add_url_rule(path, view_func=_query_promise, methods=["GET"])


@api.get("/api/reconcile")
def reconcile_overview():
    shipment_id = request.args.get("shipment_id") or request.args.get("key") or request.args.get("q")
    if shipment_id or request.args.get("waybill_id"):
        return _query_promise()
    ledger_store = store()
    payouts = ledger_store.list_payouts() if hasattr(ledger_store, "list_payouts") else []
    return jsonify({
        "payouts": payouts,
        "shipments": payouts,
        "total_cents": ledger_store.payout_total_cents() if hasattr(ledger_store, "payout_total_cents") else 0,
        "count": ledger_store.payout_count() if hasattr(ledger_store, "payout_count") else len(payouts),
    })


def _list_payouts():
    sid = request.args.get("shipment_id")
    run_id = request.args.get("run_id")
    items = store().list_payouts(shipment_id=sid, run_id=run_id) if hasattr(store(), "list_payouts") else []
    return jsonify({"items": items, "payouts": items, "data": items,
                    "total_cents": store().payout_total_cents() if hasattr(store(), "payout_total_cents") else 0,
                    "count": len(items)})


for path in ("/api/payouts", "/api/payout", "/api/shipments", "/api/settlements",
             "/api/reconcile/payouts", "/api/reconcile/shipments"):
    api.add_url_rule(path, view_func=_list_payouts, methods=["GET"])


@api.post("/api/shipments/settle")
def settle_preview():
    data = request.get_json(silent=True) or {}
    rows = data.get("rows") or data.get("shipments") or data.get("items") or []
    from app.domain.shipment import settle_shipments
    result = settle_shipments(rows)
    return jsonify(result)


@api.get("/api/examples/<profile>")
def example(profile):
    if profile not in {"clean", "dirty"}:
        # 允许读取后续新增的命名样例，但禁止路径穿越
        if not profile.replace("_", "").replace("-", "").isalnum():
            abort(404)
        candidate = SAMPLES / f"{profile}.csv"
        if not candidate.is_file():
            abort(404)
        return send_file(candidate, as_attachment=True)
    return send_file(SAMPLES / f"{profile}.csv", as_attachment=True)
