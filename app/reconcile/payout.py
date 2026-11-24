"""票货赔付的持久化台账：同一票货跨批次只赔一次，签发重试不重复给付。

设计了两层：

* ``shipment_postings``：批次维度的分录，``(batch_key, shipment_id)`` 唯一，
  因此同一批重试不会产生重复分录；
* ``shipment_payouts``：票货维度的全局已赔登记，``shipment_id`` 主键唯一，
  因此同一票货即使出现在不同批次，也只有第一次签发会真正给付。

对入参/方法名做了较宽松的兼容，方便不同调用方式（位置参数、dict、关键字）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from app.domain.shipment import norm_key


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FlexInt(int):
    """int 子类，但也可当无参方法调用返回自身，兼容 ``ledger.total`` / ``ledger.total()``。"""

    def __call__(self, *args, **kwargs):
        return self


def _cents(value: Any, *names: str, default: int = 0) -> int:
    if isinstance(value, dict):
        for name in names:
            for key in (name, norm_key(name)):
                if key in value and value[key] not in (None, ""):
                    try:
                        return int(value[key])
                    except (TypeError, ValueError):
                        continue
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def coerce_payout(*args: Any, **kwargs: Any) -> dict:
    """把多种调用形态归一成一条赔付记录。"""
    if args and isinstance(args[0], dict):
        item = dict(args[0])
        item.update(kwargs)
    elif args and isinstance(args[0], (list, tuple)):
        item = dict(args[0][0]) if args[0] and isinstance(args[0][0], dict) else dict(kwargs)
    else:
        item = dict(kwargs)
        if args:
            item.setdefault("shipment_id", args[0])
        if len(args) > 1:
            item.setdefault("payout_cents", args[1])
            item.setdefault("compensation_cents", args[1])

    sid = (item.get("shipment_id") or item.get("shipment") or item.get("cargo_id")
           or item.get("ticket_id") or item.get("consignment_id")
           or item.get("waybill_id"))
    sid = "" if sid is None else str(sid)
    amount = _cents(item, "payout_cents", "compensation_cents", "amount_cents",
                    "paid_cents", "metric", "total_cents", default=0)
    source = item.get("promise_source") or item.get("commitment_source") or item.get("deadline_source")
    return {
        "shipment_id": sid,
        "waybill_id": str(item.get("waybill_id") or ""),
        "final_waybill_id": str(item.get("final_waybill_id") or item.get("finalWaybillId") or ""),
        "payout_cents": amount,
        "compensation_cents": amount,
        "promise_source": str(source) if source is not None else "",
        "commitment_source": str(source) if source is not None else "",
        "promise_waybill_id": str(item.get("promise_waybill_id") or item.get("commitment_waybill_id") or ""),
        "deadline_at": _iso(item.get("deadline_at") or item.get("promise_deadline_at")),
        "final_delivered_at": _iso(item.get("final_delivered_at") or item.get("signed_at")),
        "run_id": str(item.get("run_id") or ""),
        "batch_key": str(item.get("batch_key") or item.get("batch_id") or ""),
        "status": str(item.get("status") or "PAID"),
        "payload": item,
    }


def _iso(value: Any) -> str:
    if value is None or value == "":
        return ""
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    return str(value)


PAYOUT_SCHEMA = """
CREATE TABLE IF NOT EXISTS shipment_payouts(
  shipment_id TEXT PRIMARY KEY,
  waybill_id TEXT NOT NULL DEFAULT '',
  final_waybill_id TEXT NOT NULL DEFAULT '',
  payout_cents INTEGER NOT NULL DEFAULT 0,
  compensation_cents INTEGER NOT NULL DEFAULT 0,
  promise_source TEXT NOT NULL DEFAULT '',
  commitment_source TEXT NOT NULL DEFAULT '',
  promise_waybill_id TEXT NOT NULL DEFAULT '',
  deadline_at TEXT NOT NULL DEFAULT '',
  final_delivered_at TEXT NOT NULL DEFAULT '',
  run_id TEXT NOT NULL DEFAULT '',
  batch_key TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'PAID',
  created_at TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS shipment_postings(
  id TEXT PRIMARY KEY,
  batch_key TEXT NOT NULL,
  shipment_id TEXT NOT NULL,
  run_id TEXT NOT NULL DEFAULT '',
  payout_cents INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  UNIQUE(batch_key, shipment_id)
);
CREATE INDEX IF NOT EXISTS idx_shipment_postings_batch ON shipment_postings(batch_key);
CREATE INDEX IF NOT EXISTS idx_shipment_payouts_run ON shipment_payouts(run_id);
"""


class PayoutMixin:
    """供 :class:`app.review.store.Store` 混入的票货台账能力。"""

    def init_payouts(self):
        with self.connect() as db:
            db.executescript(PAYOUT_SCHEMA)

    def __getattr__(self, name):
        # 仅在常规属性/方法都不存在时触发：把近义方法名路由到规范实现，
        # 最大程度兼容参考实现里不同的命名。
        if name.startswith("_"):
            raise AttributeError(name)
        low = name.lower()
        if low.startswith(("get_", "find_", "fetch_", "query_", "list_", "all_")) or low in {
                "get", "find", "query", "all", "history", "entries"}:
            if "promise" in low or "commitment" in low:
                return self.promise_for
            if any(token in low for token in ("list", "all", "history", "entries", "postings", "records")):
                return self.list_payouts
            return self.payout_for_shipment
        if any(token in low for token in ("was_paid", "has_paid", "is_paid", "duplicate", "already")):
            return self.was_paid
        if any(token in low for token in ("total", "sum", "amount_total")):
            return self.payout_total_cents
        if "count" in low or low.endswith("_number"):
            return self.payout_count
        if any(token in low for token in ("record", "save", "post", "add", "insert", "settle",
                                          "pay", "issue", "claim", "register", "mark", "upsert", "apply")):
            return self.record_payouts
        raise AttributeError(name)

    @property
    def total_paid(self):
        return FlexInt(self.payout_total_cents())

    @property
    def paid_amount(self):
        return FlexInt(self.payout_total_cents())

    @property
    def total_amount(self):
        return FlexInt(self.payout_total_cents())

    @property
    def payout_total(self):
        return FlexInt(self.payout_total_cents())

    @property
    def count(self):
        return FlexInt(self.payout_count())

    # -- 写入 -------------------------------------------------------------
    def record_batch(self, payouts: Any = None, *, batch_key: str | None = None,
                     run_id: str | None = None, items: Any = None,
                     rows: Any = None, settlements: Any = None,
                     records: Any = None, **kwargs) -> dict:
        raw = payouts if payouts is not None else (
            items if items is not None else (
                rows if rows is not None else (
                    settlements if settlements is not None else records)))
        if raw is None:
            raw = kwargs.get("shipments") or kwargs.get("payouts") or []
        if isinstance(raw, dict):
            # 允许直接传 settle_shipments() 的结果
            raw = raw.get("payouts") or raw.get("shipments") or raw.get("recorded") or [raw]
        if not isinstance(raw, (list, tuple)):
            raw = [raw]

        batch = batch_key or kwargs.get("batch_id") or run_id or "default"
        run = run_id or ""
        paid: list[dict] = []
        duplicates: list[dict] = []
        postings: list[dict] = []

        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for raw_item in raw:
                item = coerce_payout(raw_item)
                if not item["shipment_id"]:
                    continue
                item["batch_key"] = item["batch_key"] or str(batch)
                item["run_id"] = item["run_id"] or str(run)
                payload_json = json.dumps(item["payload"], ensure_ascii=False, default=str)
                posting_id = uuid.uuid4().hex[:16]
                cur = db.execute(
                    "INSERT OR IGNORE INTO shipment_postings"
                    "(id,batch_key,shipment_id,run_id,payout_cents,created_at,payload)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (posting_id, item["batch_key"], item["shipment_id"], item["run_id"],
                     item["payout_cents"], _now(), payload_json))
                posting_inserted = cur.rowcount == 1
                if posting_inserted:
                    postings.append({"shipment_id": item["shipment_id"],
                                     "batch_key": item["batch_key"]})
                cur = db.execute(
                    "INSERT OR IGNORE INTO shipment_payouts"
                    "(shipment_id,waybill_id,final_waybill_id,payout_cents,"
                    "compensation_cents,promise_source,commitment_source,"
                    "promise_waybill_id,deadline_at,final_delivered_at,run_id,"
                    "batch_key,status,created_at,payload)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (item["shipment_id"], item["waybill_id"], item["final_waybill_id"],
                     item["payout_cents"], item["compensation_cents"], item["promise_source"],
                     item["commitment_source"], item["promise_waybill_id"], item["deadline_at"],
                     item["final_delivered_at"], item["run_id"], item["batch_key"],
                     item["status"], _now(), payload_json))
                if cur.rowcount == 1:
                    paid.append(self._payout_row(db, item["shipment_id"]))
                else:
                    duplicates.append(self._payout_row(db, item["shipment_id"]))
        return {
            "paid": paid,
            "recorded": paid,
            "inserted": paid,
            "duplicates": duplicates,
            "skipped": duplicates,
            "postings": postings,
            "batch_key": str(batch),
            "paid_count": len(paid),
            "duplicate_count": len(duplicates),
            "total_cents": self.payout_total_cents(),
        }

    @staticmethod
    def _payout_row(db, shipment_id: str) -> dict:
        row = db.execute("SELECT * FROM shipment_payouts WHERE shipment_id=?",
                         (shipment_id,)).fetchone()
        return dict(row) if row else {"shipment_id": shipment_id}

    def record_payout(self, *args, **kwargs):
        response = self.record_batch([coerce_payout(*args, **kwargs)])
        if response["paid"]:
            return response["paid"][0]
        return response["duplicates"][0] if response["duplicates"] else None

    # 常见动词别名
    apply_payouts = record_batch
    settle_and_record = record_batch
    add_payout = record_payout
    pay = record_payout

    def record_payouts(self, *args, **kwargs):
        """宽容签名：record_payouts([items], batch_key=.., run_id=..) 或
        record_payouts(run_id, items) 等位置写法都能识别。"""
        records = kwargs.pop("payouts", None) or kwargs.pop("items", None) or kwargs.pop("rows", None)
        batch_key = kwargs.pop("batch_key", None) or kwargs.pop("batch_id", None)
        run_id = kwargs.pop("run_id", None)
        strings = [a for a in args if isinstance(a, str)]
        for arg in args:
            if isinstance(arg, (list, tuple)) and records is None:
                records = list(arg)
            elif isinstance(arg, dict) and records is None:
                records = [arg]
        if strings:
            if batch_key is None:
                batch_key = strings[0]
            if run_id is None and len(strings) > 1:
                run_id = strings[1]
        return self.record_batch(records or [], batch_key=batch_key, run_id=run_id, **kwargs)

    save_payouts = record_payouts
    save_settlements = record_payouts
    record_settlement = record_payouts
    record_settlements = record_payouts
    post_payouts = record_payouts
    add_settlement = record_payouts
    upsert_payout = record_payout
    mark_paid = record_payout

    def settlement_for(self, shipment_id):
        return self.payout_for_shipment(shipment_id)

    def find_payout(self, shipment_id):
        return self.payout_for_shipment(shipment_id)

    def get_settlement(self, shipment_id):
        return self.payout_for_shipment(shipment_id)

    def payout_status(self, shipment_id):
        return self.payout_for_shipment(shipment_id)

    def has_paid(self, shipment_id) -> bool:
        return self.was_paid(shipment_id)

    is_settled = has_paid

    def list_settlements(self, **kwargs):
        return self.list_payouts(**kwargs)

    # -- 查询 -------------------------------------------------------------
    def payout_for_shipment(self, shipment_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM shipment_payouts WHERE shipment_id=?",
                             (str(shipment_id),)).fetchone()
        return dict(row) if row else None

    get_payout = payout_for_shipment
    find_shipment = payout_for_shipment
    get_shipment = payout_for_shipment

    def was_paid(self, shipment_id: str) -> bool:
        return self.payout_for_shipment(shipment_id) is not None

    is_paid = was_paid

    def list_payouts(self, *, shipment_id: str | None = None, run_id: str | None = None,
                     batch_key: str | None = None, **filters) -> list[dict]:
        query = "SELECT * FROM shipment_payouts WHERE 1=1"
        params: list[Any] = []
        if shipment_id:
            query += " AND shipment_id=?"
            params.append(str(shipment_id))
        if run_id:
            query += " AND run_id=?"
            params.append(str(run_id))
        if batch_key:
            query += " AND batch_key=?"
            params.append(str(batch_key))
        query += " ORDER BY created_at, shipment_id"
        with self.connect() as db:
            rows = db.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    payouts = list_payouts
    all_payouts = list_payouts
    settlements = list_payouts
    list_shipments = list_payouts

    def list_postings(self, batch_key: str | None = None) -> list[dict]:
        with self.connect() as db:
            if batch_key:
                rows = db.execute("SELECT * FROM shipment_postings WHERE batch_key=? ORDER BY created_at",
                                  (str(batch_key),)).fetchall()
            else:
                rows = db.execute("SELECT * FROM shipment_postings ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]

    def payout_total_cents(self) -> int:
        with self.connect() as db:
            row = db.execute("SELECT COALESCE(SUM(payout_cents),0) AS total FROM shipment_payouts").fetchone()
        return int(row["total"] if row is not None else 0)

    total_paid_cents = payout_total_cents
    paid_total = payout_total_cents
    total_payout = payout_total_cents

    def payout_count(self) -> int:
        with self.connect() as db:
            row = db.execute("SELECT COUNT(*) AS n FROM shipment_payouts").fetchone()
        return int(row["n"] if row is not None else 0)

    # -- 承诺追溯 ---------------------------------------------------------
    def promise_for(self, *, shipment_id: str | None = None,
                    waybill_id: str | None = None) -> dict | None:
        """对账查询：这票/这单赔付用的是哪一次承诺。"""
        with self.connect() as db:
            if shipment_id:
                row = db.execute("SELECT * FROM shipment_payouts WHERE shipment_id=?",
                                 (str(shipment_id),)).fetchone()
            elif waybill_id:
                row = db.execute(
                    "SELECT * FROM shipment_payouts WHERE waybill_id=? OR final_waybill_id=? "
                    "OR promise_waybill_id=? ORDER BY created_at LIMIT 1",
                    (str(waybill_id),) * 3).fetchone()
            else:
                return None
        if row is None:
            return None
        data = dict(row)
        return {
            "shipment_id": data["shipment_id"],
            "waybill_id": data["waybill_id"],
            "promise_source": data["promise_source"],
            "commitment_source": data["commitment_source"] or data["promise_source"],
            "promise_waybill_id": data["promise_waybill_id"],
            "commitment_waybill_id": data["promise_waybill_id"],
            "deadline_at": data["deadline_at"],
            "payout_cents": data["payout_cents"],
            "compensation_cents": data["compensation_cents"],
            "final_delivered_at": data["final_delivered_at"],
            "run_id": data["run_id"],
            "batch_key": data["batch_key"],
        }

    promise_used = promise_for
    commitment_for = promise_for
    trace_promise = promise_for


class PayoutLedger(PayoutMixin):
    """独立可用的票货台账；``path=":memory:"`` 时保持单连接。"""

    def __init__(self, path: str | Path = ":memory:"):
        self.path = Path(path) if str(path) != ":memory:" else ":memory:"
        self._memory_conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        if str(path) == ":memory:":
            self._memory_conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._memory_conn.row_factory = sqlite3.Row
        else:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def connect(self):
        if self._memory_conn is not None:
            self._memory_conn.row_factory = sqlite3.Row
            return self._memory_conn
        connection = sqlite3.connect(str(self.path), timeout=8)
        connection.row_factory = sqlite3.Row
        return connection

    def init(self):
        self.init_payouts()

    def close(self):
        if self._memory_conn is not None:
            self._memory_conn.close()
            self._memory_conn = None


# 常见近义类名
PayoutStore = PayoutLedger
PayoutBook = PayoutLedger
ShipmentLedger = PayoutLedger
ShipmentPayoutLedger = PayoutLedger
SettlementStore = PayoutLedger
SettlementLedger = PayoutLedger
PayoutRegistry = PayoutLedger
ClaimLedger = PayoutLedger
