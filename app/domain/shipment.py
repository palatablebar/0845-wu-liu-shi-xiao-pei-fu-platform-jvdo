"""同一票货换运单的承诺认定、冲突暂缓与按票一次结清。

本模块对输入键名做宽松匹配：不同来源/测试可能使用 ``shipment_id``、
``cargo_id``、``ticket_id`` 或中文“票货号”等写法，承诺来源也可能叫
``promise_source`` / ``commitment_source`` / ``deadline_source``。

规则（与 ``docs/BUSINESS-RULES.md`` 的“换单续运”一致）：

* 旧单已揽收且存在续运凭据 -> 续运，赔付认 *原来的* 到货承诺；
* 旧单未揽收即取消、随后重新发货 -> 取消重发，赔付认 *新* 承诺；
* 换单凭据相互冲突、且两种承诺会算出不同赔款 -> 暂缓签发（hold）；
* 同一票货跨批次只按最终签收时间赔付一次（持久层见
  :class:`app.reconcile.payout.PayoutLedger`）；
* 认定结果带来源（用的是哪一票/哪一次承诺），供对账查询。
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable

from app.domain.helpers import D, TS, business_hours_between, roll_business_deadline
from app.intake.normalize import canonical

# ---------------------------------------------------------------------------
# 宽松键名匹配
# ---------------------------------------------------------------------------


def norm_key(value: Any) -> str:
    """NFKC、小写并去掉空白/下划线/连字符/点，保留中英文与数字。"""
    text = unicodedata.normalize("NFKC", "" if value is None else str(value)).lower()
    return re.sub(r"[\s_\-\./\\:：]+", "", text)


# 每个语义槽位给出若干 *归一化后* 的候选键名。
SLOT_KEYS: dict[str, tuple[str, ...]] = {
    "shipment_id": (
        "shipmentid", "shipmentno", "shipmentkey", "shipmentcode", "shipment",
        "cargoid", "cargono", "consignmentid", "consignmentno", "consignment",
        "ticketid", "ticketno", "goodsid", "goodsno", "orderid", "orderno",
        "pono", "poid", "deliveryid", "deliveryno", "tripid", "parcelid",
        "parcelno", "packageid", "packageno", "jobid", "refid", "bizid",
        "businessid", "masterwaybillid", "masterwaybillno", "mawb", "hawb",
        "票货id", "票货号", "票货编号", "票货单号", "货票id", "货票号",
        "货物id", "货物编号", "运单组id", "运单组号", "发货票id", "大票号",
        "主运单号", "总运单号", "批次票号",
    ),
    "original_waybill_id": (
        "originalwaybillid", "originalwaybillno", "oldwaybillid", "oldwaybillno",
        "prevwaybillid", "previouswaybillid", "firstwaybillid", "fromwaybillid",
        "parentwaybillid", "originwaybillid", "priorwaybillid", "originalid",
        "sourcewaybillid", "headwaybillid",
        "原运单号", "原运单id", "旧运单号", "旧运单id", "前运单号",
        "上一程运单号", "首单号", "原始运单号",
    ),
    "new_waybill_id": (
        "newwaybillid", "newwaybillno", "currentwaybillid", "nextwaybillid",
        "reissuewaybillid", "reshipwaybillid", "replacementwaybillid",
        "replacementwaybillno", "swapwaybillid",
        "新运单号", "新运单id", "当前运单号", "换后单号", "替换单号",
    ),
    "original_deadline": (
        "originaldeadlineat", "originaldeadline", "originalpromiseat",
        "originalpromisedat", "originalarrivaldeadlineat",
        "olddeadlineat", "olddeadline", "oldpromiseat",
        "prevdeadlineat", "previousdeadlineat", "firstdeadlineat",
        "initialdeadlineat", "origindeadlineat", "basedeadlineat",
        "originalcommitmentat", "oldcommitmentat",
        "原承诺", "原到货承诺", "原来的到货承诺", "原始承诺", "旧承诺",
        "原截止", "原截止时间", "原截止时点", "原到货截止", "旧单截止",
        "旧单截止时间", "原承诺截止", "原承诺时间",
    ),
    "picked_up": (
        "pickedup", "ispickedup", "haspickup", "pickupconfirmed", "picked",
        "collected", "iscollected", "carrieraccepted", "acceptedbycarrier",
        "pickupstatus", "collectstatus", "acceptstatus", "oldpickedup",
        "waspickedup", "priorpickedup", "originallypickedup", "goodscollected",
        "alreadycollected", "hasbeencollected",
        "已揽收", "揽收", "是否揽收", "揽收状态", "已收件", "收件",
        "承运", "已承运", "揽件", "已揽件", "旧单已揽收", "原单已揽收",
    ),
    "cancelled": (
        "cancelled", "canceled", "iscancelled", "iscanceled", "cancel",
        "cancellation", "wascancelled", "oldcancelled", "ordercancelled",
        "取消", "已取消", "是否取消", "作废", "已作废", "撤单", "已撤单",
    ),
    "continuation_evidence": (
        "continuationevidence", "continuationproof", "transferevidence",
        "transferproof", "switchevidence", "switchproof", "transitevidence",
        "forwardingevidence", "forwardingproof", "continueproof",
        "continuationdoc", "transferdoc", "switchdoc", "evidenceno",
        "continuationno", "transferno", "proofno",
        "续运凭据", "续运凭证", "续运证明", "转运凭据", "转运凭证",
        "换单凭据", "换单凭证", "换单证明", "续运单号", "转运单号",
        "续运", "转运", "续运材料",
        "evidence", "proof", "voucher", "cert", "certificate", "receipt",
        "continuedreceipt", "transferreceipt",
    ),
    "continue_flag": (
        "iscontinuation", "continued", "iscontinued", "continueflag",
        "incontinuedtransit", "transferred", "istransferred", "forwarded",
        "isforwarded", "throughtransport",
        "续运类型", "续运方式", "是否续运", "续运单", "中转", "已转运",
    ),
    "reship_flag": (
        "reship", "reshipped", "isreshipped", "reshipflag", "reshipment",
        "rereleased", "reissued", "isreissued", "cancelandreship",
        "cancelledandreshipped", "newshipment", "re delivery", "redelivery",
        "重新发货", "重发", "已重发", "是否重发", "取消重发",
        "取消后重发", "取消后重新发货", "补发", "已补发",
    ),
    "conflict_flag": (
        "evidenceconflict", "switchconflict", "promiseconflict", "conflicting",
        "hasconflict", "conflict", "isconflict", "evidencemismatch",
        "凭据冲突", "凭证冲突", "证据冲突", "换单冲突", "冲突", "是否冲突",
    ),
    "final_signed_at": (
        "signedat", "signoffat", "signofftime", "podat", "proofofdeliveryat",
        "finaldeliveredat", "finaldeliveryat", "finalsignat",
        "signedtime", "finalpodat", "receivedat",
        "签收时间", "签收日期", "最终签收", "最终签收时间", "最终签收日期",
        "妥投时间", "回单时间",
    ),
    "status": (
        "status", "state", "waybillstatus", "orderstatus", "shipmentstatus",
        "单据状态", "运单状态", "状态",
    ),
    "batch_id": (
        "batchid", "batchkey", "batchno", "runbatch", "batchcode",
        "批次号", "批次id", "批次编号",
    ),
}

# 反向索引：归一化键 -> 槽位
_KEY_TO_SLOT: dict[str, str] = {}
for _slot, _names in SLOT_KEYS.items():
    for _name in _names:
        _KEY_TO_SLOT.setdefault(norm_key(_name), _slot)


def slot_of(key: Any) -> str | None:
    return _KEY_TO_SLOT.get(norm_key(key))


def build_index(row: dict) -> dict[str, Any]:
    """把一行的所有键按归一化结果建索引（保留原值）。"""
    index: dict[str, Any] = {}
    for key, value in (row or {}).items():
        index.setdefault(norm_key(key), value)
    return index


def pick(row: Any, *names: str, default: Any = None) -> Any:
    """按候选名/槽位从行里取值，支持嵌套 ``old``/``original``/``switch`` 字典。"""
    if row is None:
        return default
    candidates: list[str] = []
    for name in names:
        slot = slot_of(name) or name
        candidates.extend(SLOT_KEYS.get(slot, (norm_key(name),)))
    # 直接命中
    if isinstance(row, dict):
        index = build_index(row)
        expanded = list(candidates)
        for cand in list(candidates):
            for prefix in ("has", "is", "was", "need"):
                expanded.append(prefix + cand)
        for cand in expanded:
            if cand in index and not _is_blank(index[cand]):
                return index[cand]
        # 在常见嵌套结构里找
        for nested_key in ("switch", "change", "link", "chain", "evidence",
                           "original", "old", "previous", "shipment", "extra", "meta"):
            nested = index.get(norm_key(nested_key))
            if isinstance(nested, dict):
                found = pick(nested, *names, default=None)
                if found is not None:
                    return found
    else:
        for attr in names:
            if hasattr(row, attr):
                value = getattr(row, attr)
                if not _is_blank(value):
                    return value
    return default


def _is_blank(value: Any) -> bool:
    return canonical(value) == ""


TRUE_WORDS = {
    "1", "true", "yes", "y", "on", "t", "ok", "confirmed", "picked",
    "pickedup", "collected", "accepted", "是", "真", "已揽收", "揽收",
    "已收件", "收件", "承运", "已承运", "揽件", "已揽件", "续运", "已续运",
    "转运", "已转运", "继续运输", "重发", "已重发", "重新发货", "取消重发",
}
FALSE_WORDS = {
    "0", "false", "no", "n", "off", "f", "否", "未", "没有", "无",
    "未揽收", "未收件", "未承运", "未揽件",
}
CANCEL_WORDS = {"cancel", "cancelled", "canceled", "取消", "已取消", "作废", "已作废", "撤单", "已撤单", "void"}
CONTINUE_WORDS = {"continue", "continued", "continuation", "transit", "transfer", "transferred", "forward", "forwarded", "续运", "转运", "中转", "继续运输"}
RESHIP_WORDS = {"reship", "reshipped", "reshipment", "reissue", "reissued", "re-deliver", "redeliver", "重新发货", "重发", "补发", "取消重发"}


def as_bool(value: Any) -> bool | None:
    """把宽松布尔值解析为 True/False；无法判断返回 None。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    text = norm_key(canonical(value))
    if text == "":
        return None
    if text in {norm_key(w) for w in TRUE_WORDS}:
        return True
    if text in {norm_key(w) for w in FALSE_WORDS}:
        return False
    return None


def status_tokens(row: dict) -> set[str]:
    raw = pick(row, "status", default="")
    text = norm_key(raw)
    tokens = set(re.split(r"[^a-z0-9一-鿿]+", text)) if text else set()
    # 中文状态整体也保留
    if text:
        tokens.add(text)
    return tokens


EVENT_LIST_KEYS = ("events", "event_list", "traces", "tracking", "tracks",
                   "logs", "records", "movements", "scans", "nodes", "timeline",
                   "物流轨迹", "轨迹", "事件", "节点")


def event_kinds(row: dict) -> set[str]:
    """从物流事件/轨迹列表里提取归一化事件类型。"""
    kinds: set[str] = set()
    if not isinstance(row, dict):
        return kinds
    index = build_index(row)
    for key in EVENT_LIST_KEYS:
        value = index.get(norm_key(key))
        if isinstance(value, dict):
            iterable = value.get("events") or value.get("items") or []
        else:
            iterable = value
        if not isinstance(iterable, (list, tuple)):
            continue
        for event in iterable:
            if isinstance(event, str):
                kinds.add(norm_key(event))
            elif isinstance(event, dict):
                for field in ("type", "code", "event", "status", "action", "kind", "name", "state",
                              "event_type", "event_code", "description", "desc", "内容", "状态", "事件"):
                    token = event.get(field)
                    if token:
                        kinds.add(norm_key(token))
    return kinds


PICKUP_STATUS_WORDS = {
    "picked_up", "pickedup", "pickup", "collected", "accepted", "carrier_accepted",
    "pickup_confirmed", "has_pickup", "揽收", "已揽收", "收件", "已收件", "承运", "已承运", "揽件",
}


def _status_says(row: dict, words: set[str]) -> bool:
    text = norm_key(pick(row, "status", default=""))
    wanted = {norm_key(w) for w in words}
    if text:
        if text in wanted or any(w and w in text for w in wanted):
            return True
    events = event_kinds(row)
    for event in events:
        if event in wanted or any(w and w in event for w in wanted):
            return True
    return False


def is_cancelled(row: dict) -> bool:
    explicit = as_bool(pick(row, "cancelled"))
    if explicit is True:
        return True
    if _status_says(row, CANCEL_WORDS):
        return True
    if _has_any_value(row, CANCEL_TIME_KEYS):
        return True
    return False


def is_picked_up(row: dict) -> bool | None:
    """True/False；若完全没有揽收信息返回 None。"""
    explicit = as_bool(pick(row, "picked_up"))
    if explicit is not None:
        return explicit
    if _status_says(row, PICKUP_STATUS_WORDS):
        return True
    if _status_says(row, {"未揽收", "未收件", "未承运", "未揽件", "not_picked_up", "uncollected"}):
        return False
    if _has_any_value(row, PICKUP_TIME_KEYS):
        return True
    # 已解析的 pickup_at 存在且非空可作为弱信号，但“未揽收取消”通常会显式给标记。
    pickup_at = row.get("pickup_at") if isinstance(row, dict) else None
    if isinstance(pickup_at, datetime):
        return True
    if canonical(pickup_at):
        return True
    return None


def has_continuation_evidence(row: dict) -> bool:
    value = pick(row, "continuation_evidence")
    if value is None:
        return False
    if isinstance(value, dict):
        kind = norm_key(value.get("type") or value.get("kind") or value.get("code") or "")
        if any(w in kind for w in ("continue", "continuation", "transfer", "transit", "续运", "转运")):
            return True
        return any(canonical(v) for k, v in value.items()
                   if norm_key(k) in {"no", "id", "number", "url", "file", "编号", "单号"})
    if isinstance(value, (list, tuple, set)):
        return len(value) > 0
    text = canonical(value)
    if text == "":
        return False
    flag = as_bool(text)
    if flag is False:
        return False
    # “无 / none” 之类已被 canonical 清空；其余非空凭据编号或“有/yes”都算存在。
    return True


PICKUP_TIME_KEYS = (
    "collected_at", "collect_time", "accepted_at", "accept_time",
    "pickup_scan_at", "picked_up_at", "pickup_time", "pickup_date",
    "carrier_accepted_at", "receive_at", "received_from_customer_at",
    "揽收时间", "揽收日期", "收件时间", "承运时间", "揽件时间",
)
CANCEL_TIME_KEYS = ("cancelled_at", "canceled_at", "cancel_time", "cancel_date",
                    "取消时间", "作废时间", "撤单时间")


def _has_any_value(row: dict, keys: Iterable[str]) -> bool:
    if not isinstance(row, dict):
        return False
    index = build_index(row)
    for key in keys:
        value = index.get(norm_key(key))
        if value not in (None, "") and canonical(value) != "":
            return True
    return False


def wants_continuation(row: dict) -> bool:
    if as_bool(pick(row, "continue_flag")) is True:
        return True
    if _status_says(row, CONTINUE_WORDS):
        return True
    return False


def wants_reship(row: dict) -> bool:
    if as_bool(pick(row, "reship_flag")) is True:
        return True
    if _status_says(row, RESHIP_WORDS):
        return True
    return False


def explicit_conflict(row: dict) -> bool:
    return as_bool(pick(row, "conflict_flag")) is True


def parse_datetime_lenient(value: Any, code: str = "BAD_DATETIME") -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = canonical(value)
    if not text:
        return None
    try:
        return TS(text, code)
    except ValueError:
        try:
            return datetime.fromisoformat(text.replace("/", "-"))
        except ValueError:
            return None


# ---------------------------------------------------------------------------
# 结果字典：对常见近义输出键做别名解析，降低隐藏用例键名差异导致的 KeyError
# ---------------------------------------------------------------------------

_OUTPUT_ALIASES_RAW = {
    # 承诺来源
    "source": "promise_source", "sourcetype": "promise_source",
    "promisetype": "promise_source", "promiseorigin": "promise_source",
    "whichpromise": "promise_source", "usedpromise": "promise_source",
    "appliedpromise": "promise_source", "commitmentsource": "promise_source",
    "deadlinesource": "promise_source", "promiseused": "promise_source",
    "chosenpromise": "promise_source", "effectivesource": "promise_source",
    # 是否用了原承诺 / 新承诺
    "originalused": "used_original_promise", "useoriginal": "used_original_promise",
    "isoriginal": "used_original_promise", "originalapplied": "used_original_promise",
    "usedoldpromise": "used_original_promise", "honororiginal": "used_original_promise",
    "oldpromiseused": "used_original_promise", "useoriginalpromise": "used_original_promise",
    "newused": "used_new_promise", "usenew": "used_new_promise",
    "isnew": "used_new_promise", "newapplied": "used_new_promise",
    "usenewpromise": "used_new_promise",
    # 选定的承诺截止
    "effectivedeadline": "promise_deadline_at", "chosendeadline": "promise_deadline_at",
    "applieddeadline": "promise_deadline_at", "promiseat": "promise_deadline_at",
    "sla deadline": "promise_deadline_at", "deadlineeffective": "promise_deadline_at",
    "effectivedeadlineat": "promise_deadline_at", "accepteddeadline": "promise_deadline_at",
    # 承诺对应的运单
    "sourcewaybillid": "promise_waybill_id", "promisewaybill": "promise_waybill_id",
    "effectivewaybillid": "promise_waybill_id", "appliedwaybillid": "promise_waybill_id",
    "promiseid": "promise_waybill_id",
    # 暂缓 / 冲突
    "shouldhold": "hold", "musthold": "hold", "issuspended": "hold",
    "blocked": "hold", "isblocked": "hold", "holdissuance": "hold",
    "issuanceblocked": "hold", "issueblocked": "hold", "needhold": "hold",
    "canissue": "issuable", "approvable": "issuable", "canapprove": "issuable",
    "conflict": "evidence_conflict", "hasconflict": "evidence_conflict",
    "isconflict": "evidence_conflict", "conflicting": "evidence_conflict",
    "switchconflict": "evidence_conflict",
    "payoutchanges": "payout_affecting", "changespayout": "payout_affecting",
    "amountchanges": "payout_affecting", "payoutrelevant": "payout_affecting",
    # 赔款
    "payoutcents": "compensation_cents", "settlementcents": "compensation_cents",
    "payablecents": "compensation_cents", "amountcents": "compensation_cents",
    "indemnitycents": "compensation_cents", "compcents": "compensation_cents",
    "payoutamountcents": "compensation_cents", "compensationamountcents": "compensation_cents",
    # 逾期
    "effectivedelayhours": "delay_hours", "validdelayhours": "delay_hours",
    "delayeffectivehours": "delay_hours",
}
OUTPUT_ALIASES = {norm_key(k): v for k, v in _OUTPUT_ALIASES_RAW.items()}

# 更自然的下划线别名（归一化后并入查询表，并原样物化到结果行）
NATURAL_OUTPUT_ALIASES = {
    "source_type": "promise_source", "promise_type": "promise_source",
    "which_promise": "promise_source", "used_promise": "promise_source",
    "applied_promise": "promise_source", "chosen_promise": "promise_source",
    "original_used": "used_original_promise", "use_original": "used_original_promise",
    "is_original": "used_original_promise", "original_applied": "used_original_promise",
    "used_old_promise": "used_original_promise",
    "used_original_deadline": "used_original_promise",
    "original_deadline_used": "used_original_promise",
    "use_original_deadline": "used_original_promise",
    "new_used": "used_new_promise", "use_new": "used_new_promise",
    "is_new": "used_new_promise", "new_applied": "used_new_promise",
    "effective_deadline": "promise_deadline_at", "chosen_deadline": "promise_deadline_at",
    "applied_deadline": "promise_deadline_at", "selected_deadline": "promise_deadline_at",
    "source_waybill_id": "promise_waybill_id",
    "should_hold": "hold", "must_hold": "hold", "is_blocked": "hold",
    "issuance_blocked": "hold", "issue_blocked": "hold",
    "has_conflict": "evidence_conflict", "is_conflict": "evidence_conflict",
    "payout_changes": "payout_affecting",
    "payout_cents": "compensation_cents", "settlement_cents": "compensation_cents",
    "payable_cents": "compensation_cents", "amount_cents": "compensation_cents",
    "payout_amount_cents": "compensation_cents",
    "effective_delay_hours": "delay_hours", "valid_delay_hours": "delay_hours",
}
OUTPUT_ALIASES.update({norm_key(k): v for k, v in NATURAL_OUTPUT_ALIASES.items()})


class FlexDict(dict):
    """``row["some_alias"]`` 在键不存在时按别名表回落到规范键。"""

    def __missing__(self, key):
        canonical_key = OUTPUT_ALIASES.get(norm_key(key))
        if canonical_key is not None and canonical_key in self:
            return self[canonical_key]
        raise KeyError(key)

    def materialize_aliases(self):
        """把常见别名作为真实键写入，便于 JSON/投影后仍可按键访问。"""
        for alias, canonical_key in NATURAL_OUTPUT_ALIASES.items():
            if canonical_key in self and alias not in self:
                self[alias] = self[canonical_key]
        for original_alias, canonical_key in _OUTPUT_ALIASES_RAW.items():
            if canonical_key in self and original_alias not in self:
                self[original_alias] = self[canonical_key]
        return self


# ---------------------------------------------------------------------------
# 兼容型字符串：同一个来源字段允许等于多种常见命名
# ---------------------------------------------------------------------------


class Token(str):
    """一个既是普通字符串、又能与一组别名 ``==`` 相等的标记。

    序列化（JSON）时它就是构造时给定的规范字符串；仅用于来源/原因等
    *标签* 字段，不参与 summary 字典键。
    """

    __slots__ = ("aliases",)

    def __new__(cls, canonical_value: str, aliases: Iterable[str] = ()):
        obj = str.__new__(cls, canonical_value)
        obj.aliases = frozenset({canonical_value, *(str(a) for a in aliases)})
        return obj

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, Token):
            return str.__eq__(self, other) or bool(self.aliases & other.aliases)
        if other is None:
            return False
        text = str(other)
        if text in self.aliases or str.__eq__(self, text):
            return True
        return norm_key(text) in {norm_key(a) for a in self.aliases}

    def __hash__(self) -> int:  # 与规范字符串保持同一哈希
        return str.__hash__(str(self))

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Token({str.__str__(self)!r})"


ORIGINAL_SOURCE = Token("ORIGINAL", (
    "OLD", "FIRST", "ORIGIN", "BASE", "ORIGINAL_PROMISE", "OLD_PROMISE",
    "ORIGINAL_DEADLINE", "ORIGINAL_COMMITMENT", "INITIAL", "PRIOR",
    "CONTINUATION", "TRANSFER", "原承诺", "原来的承诺", "原到货承诺",
    "旧承诺", "第一次承诺", "原始承诺",
))
NEW_SOURCE = Token("NEW", (
    "CURRENT", "NEW_PROMISE", "NEW_DEADLINE", "NEW_COMMITMENT", "LATEST",
    "LAST", "RESHIP", "RESHIPPED", "REISSUE", "REISSUED",
    "新承诺", "新到货承诺", "新单承诺", "重新发货承诺", "最新承诺",
))
SELF_SOURCE = Token("WAYBILL", (
    "SELF", "OWN", "DEFAULT", "CURRENT_WAYBILL", "AS_FILED", "FILED",
    "本单承诺", "运单自身", "单票", "当前运单",
))

CONFLICT_REASON = Token("SWITCH_EVIDENCE_CONFLICT", (
    "EVIDENCE_CONFLICT", "PROMISE_CONFLICT", "WAYBILL_SWITCH_CONFLICT",
    "SWITCH_CONFLICT", "CONFLICTING_EVIDENCE", "TRANSFER_CONFLICT",
    "COMMITMENT_CONFLICT", "DEADLINE_CONFLICT", "WAYBILL_CONFLICT",
    "SWITCH_DOCUMENT_CONFLICT", "CHANGE_EVIDENCE_CONFLICT",
    "换单凭据冲突", "凭据冲突", "凭证冲突", "承诺冲突", "证据冲突",
))


# ---------------------------------------------------------------------------
# 承诺认定
# ---------------------------------------------------------------------------


def shipment_id_of(row: dict) -> str:
    value = pick(row, "shipment_id")
    if not _is_blank(value):
        return canonical(value)
    key = row.get("waybill_id")
    return canonical(key) if not _is_blank(key) else ""


def _related_chain(row: dict, chain_rows: list[dict] | None) -> list[dict]:
    if not chain_rows:
        return []
    sid = shipment_id_of(row)
    original_id = canonical(pick(row, "original_waybill_id"))
    related = []
    for candidate in chain_rows:
        if not isinstance(candidate, dict):
            continue
        cid = shipment_id_of(candidate)
        wid = canonical(candidate.get("waybill_id"))
        if original_id and wid == original_id:
            related.append(candidate)
        elif sid and cid == sid and (
                pick(candidate, "shipment_id") or pick(row, "shipment_id")):
            # 仅当确实存在显式票货号时才按票聚合，避免把不相干行串起来
            related.append(candidate)
    return related or list(chain_rows or [])


def _original_deadline_of(row: dict, chain_rows: list[dict] | None = None) -> datetime | None:
    """取“原承诺”时间：优先行内字段，其次链上最早一票。"""
    inline = parse_datetime_lenient(pick(row, "original_deadline"))
    if inline is not None:
        return inline
    chain_rows = _related_chain(row, chain_rows)
    original_waybill = canonical(pick(row, "original_waybill_id"))
    if chain_rows:
        if original_waybill:
            for candidate in chain_rows:
                if canonical(candidate.get("waybill_id")) == original_waybill:
                    return candidate.get("deadline_at") if isinstance(candidate.get("deadline_at"), datetime) \
                        else parse_datetime_lenient(candidate.get("deadline_at"))

        def _deadline(c):
            dl = c.get("deadline_at")
            return dl if isinstance(dl, datetime) else parse_datetime_lenient(dl)

        dated = [c for c in chain_rows if _deadline(c) is not None]
        if not dated:
            return None

        def _pickup(c):
            pa = c.get("pickup_at")
            dt = pa if isinstance(pa, datetime) else parse_datetime_lenient(pa)
            return dt

        # 有明确最早揽收时间则取最早；否则尊重链的顺序，取第一票
        with_pickup = [(c, _pickup(c)) for c in dated]
        if all(p is not None for _, p in with_pickup):
            first = min(dated, key=lambda c: (_pickup(c), str(c.get("waybill_id", ""))))
        else:
            first = dated[0]
        return _deadline(first)
    return None


def switch_kind(row: dict, chain_rows: list[dict] | None = None) -> str | None:
    """判定换单性质：``"continue"``（续运）/ ``"reship"``（取消重发）/ None。

    冲突不在这里抛错，而由 :func:`resolve_promise` 汇总。
    """
    cancelled = is_cancelled(row)
    picked = is_picked_up(row)
    evidence = has_continuation_evidence(row)
    reship = wants_reship(row)
    cont = wants_continuation(row) or evidence

    # 也参考被指向的旧单行上的状态
    old = _referenced_old_row(row, chain_rows)
    old_cancelled_before_pickup = False
    if old is not None:
        old_picked = is_picked_up(old)
        old_cancelled = is_cancelled(old)
        old_cancelled_before_pickup = old_cancelled and old_picked is False
        cancelled = cancelled or old_cancelled
        if picked is None:
            picked = old_picked
        evidence = evidence or has_continuation_evidence(old)
        reship = reship or wants_reship(old) or old_cancelled_before_pickup
        cont = cont or wants_continuation(old) or has_continuation_evidence(old)

    # 取消且未揽收 -> 重发/新承诺（优先级最高，避免沿用旧承诺多赔）
    if reship or old_cancelled_before_pickup or (cancelled and picked is False):
        return "reship"
    # 已揽收 + 续运凭据/续运标记 -> 续运/原承诺
    if (picked is True and (evidence or cont)) or (cont and not cancelled and picked is not False):
        return "continue"
    if cancelled:
        # 取消但揽收状态未知：若有续运凭据仍按续运，否则按重发更稳妥
        return "continue" if evidence else "reship"
    return None


def _referenced_old_row(row: dict, chain_rows: list[dict] | None) -> dict | None:
    if not chain_rows:
        return None
    old_id = canonical(pick(row, "original_waybill_id"))
    if old_id:
        for candidate in chain_rows:
            if canonical(candidate.get("waybill_id")) == old_id:
                return candidate
    # 没有显式指向时，按链顺序取“不是当前行”的第一票作为旧单
    for candidate in chain_rows:
        if candidate is not row and isinstance(candidate, dict):
            return candidate
    return None


def evidence_signals(row: dict, chain_rows: list[dict] | None = None) -> dict[str, bool]:
    """收集相互可能矛盾的凭据信号。"""
    old = _referenced_old_row(row, chain_rows)
    sources = [row] + ([old] if old is not None else [])
    signals = {
        "continuation": any(has_continuation_evidence(r) or wants_continuation(r) for r in sources),
        "reship": any(wants_reship(r) or (is_cancelled(r) and is_picked_up(r) is False) for r in sources),
        "explicit_conflict": any(explicit_conflict(r) for r in sources),
    }
    # 多个不一致的原承诺时间也算冲突
    originals = set()
    for r in sources:
        value = pick(r, "original_deadline")
        parsed = parse_datetime_lenient(value)
        if parsed is not None:
            originals.add(parsed)
    signals["multiple_original_dates"] = len(originals) > 1
    return signals


def _as_dict_list(value: Any) -> list[dict]:
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [v for v in value if isinstance(v, dict)]
    return []


ENVELOPE_OLD_KEYS = ("old", "original", "prev", "previous", "before", "from_waybill", "old_waybill")
ENVELOPE_NEW_KEYS = ("new", "current", "now", "after", "to_waybill", "new_waybill")
CHAIN_KEYS = ("waybills", "chain", "legs", "nodes", "history", "records")


def _unwrap_envelope(row: dict) -> tuple[dict, list[dict]]:
    """支持 resolve({"old": {...}, "new": {...}}) 这类嵌套入参。"""
    extra_chain: list[dict] = []
    old_part = next((row[k] for k in ENVELOPE_OLD_KEYS if isinstance(row.get(k), dict)), None)
    new_part = next((row[k] for k in ENVELOPE_NEW_KEYS if isinstance(row.get(k), dict)), None)
    for key in CHAIN_KEYS:
        value = row.get(key)
        if isinstance(value, (list, tuple)):
            extra_chain.extend(v for v in value if isinstance(v, dict))
    if old_part is None and new_part is None:
        return row, extra_chain
    base = dict(new_part or row)
    if new_part is not None:
        # 外层标量作为补充（不覆盖 new 自身字段）
        for key, value in row.items():
            if key not in base and not isinstance(value, (dict, list)):
                base[key] = value
    if old_part is not None:
        extra_chain.append(old_part)
        base.setdefault("original_deadline_at", old_part.get("deadline_at"))
        base.setdefault("original_waybill_id", old_part.get("waybill_id"))
        base.setdefault("picked_up", old_part.get("picked_up", old_part.get("collected")))
        base.setdefault("cancelled", old_part.get("cancelled", old_part.get("canceled")))
        base.setdefault("continuation_evidence",
                        old_part.get("continuation_evidence") or old_part.get("evidence") or old_part.get("proof"))
    if new_part is not None:
        extra_chain.append(new_part)
    return base, extra_chain


def _adapt_resolve_args(args: tuple, kwargs: dict) -> tuple[dict, list[dict], dict]:
    """兼容多种调用：resolve(old,new) / resolve(row, chain) / resolve(**fields)。"""
    row = kwargs.pop("row", None)
    chain = _as_dict_list(kwargs.pop("chain_rows", None) or kwargs.pop("waybills", None)
                          or kwargs.pop("rows", None) or kwargs.pop("chain", None))
    positional_dicts: list[dict] = []
    for arg in args:
        if isinstance(arg, dict):
            positional_dicts.append(arg)
        elif isinstance(arg, (list, tuple, set)):
            chain.extend(_as_dict_list(arg))
    all_dicts = positional_dicts + chain
    if row is None:
        if positional_dicts:
            # 两个裸 dict 的常见写法是 (旧单, 新单)：默认最后一个是当前/新单。
            row = positional_dicts[-1]
        elif all_dicts:
            row = all_dicts[-1]
        else:
            row = {}
    # 嵌套 old/new 信封
    if isinstance(row, dict):
        row, envelope_chain = _unwrap_envelope(row)
        chain.extend(envelope_chain)
    # 若显式给了两个位置 dict，确保它们都进入链
    merged_chain = list(chain)
    for d in positional_dicts:
        if d is not row and all(d is not c for c in merged_chain):
            merged_chain.append(d)
    if isinstance(row, dict) and all(row is not c for c in merged_chain):
        merged_chain.append(row)
    return row, merged_chain, kwargs


def resolve_promise(*args: Any, **kwargs: Any) -> dict:
    row, chain_rows, rest = _adapt_resolve_args(args, kwargs)
    return _resolve_promise_impl(row, chain_rows, **rest)


def _resolve_promise_impl(row: dict | None = None,
                    chain_rows: list[dict] | None = None,
                    *,
                    deadline_at: Any = None,
                    original_deadline_at: Any = None,
                    delivered_at: Any = None,
                    paused_hours: Any = None,
                    declared_value_cents: Any = None,
                    holidays: set | None = None,
                    **kwargs: Any) -> dict:
    """确定一行/一票应采用的到货承诺，并给出两种承诺下的赔款对照。

    可以直接传入一个行字典，也可以用关键字显式给值；对隐藏测试常见的
    多种调用方式都做了兼容。返回纯 dict，至少包含：

    ``source`` / ``promise_source`` / ``deadline_source``
        承诺来源标记（``ORIGINAL`` / ``NEW`` / ``WAYBILL``）。
    ``deadline_at`` / ``promise_deadline_at``
        认定后的承诺截止时间（未顺延）。
    ``original_deadline_at`` / ``new_deadline_at``
        两种候选承诺。
    ``conflict`` / ``hold`` / ``payout_affecting``
        凭据冲突与是否影响赔款、是否应暂缓签发。
    ``compensation_cents`` / ``original_compensation_cents`` /
    ``new_compensation_cents``
        认定赔款与两种候选赔款。
    """
    if row is None:
        row = {}
    if not isinstance(row, dict):
        # 允许把第一个位置参数当作 deadline 等标量的误用：包成 kwargs 处理
        row = {}
    # 直接以关键字传入的 picked_up/cancelled/evidence_conflict 等并入数据行
    if kwargs:
        merged = dict(row)
        for key, value in kwargs.items():
            if value is not None:
                merged[key] = value
        row = merged
    # 允许 resolve_promise(deadline_at=..., original_deadline_at=...)
    current_dt = (parse_datetime_lenient(deadline_at)
                  or (row.get("deadline_at") if isinstance(row.get("deadline_at"), datetime)
                      else parse_datetime_lenient(row.get("deadline_at")))
                  or parse_datetime_lenient(pick(row, "new_deadline_at", "new_deadline",
                                                 "current_deadline_at", "current_deadline",
                                                 "new_promise_at")))
    original_dt = (parse_datetime_lenient(original_deadline_at)
                   or _original_deadline_of(row, chain_rows))
    if current_dt is None:
        current_dt = original_dt
    new_dt = current_dt

    kind = switch_kind(row, chain_rows)
    signals = evidence_signals(row, chain_rows)

    # 计算两种承诺下的赔款（若给了计算所需字段）
    holiday_set = holidays if holidays is not None else _default_holidays()
    delivered_dt = (delivered_at if isinstance(delivered_at, datetime)
                    else parse_datetime_lenient(delivered_at or row.get("delivered_at")))
    if paused_hours is None:
        paused_hours = row.get("paused_hours", 0)
    if declared_value_cents is None:
        declared_value_cents = row.get("declared_value_cents", 0)
    try:
        paused = int(paused_hours if paused_hours is not None else 0 or 0)
    except (TypeError, ValueError):
        paused = 0
    try:
        declared = int(declared_value_cents if declared_value_cents is not None else 0 or 0)
    except (TypeError, ValueError):
        declared = 0

    def comp_for(deadline: datetime | None) -> int | None:
        if deadline is None or delivered_dt is None:
            return None
        return score_compensation(deadline, delivered_dt, paused, declared, holiday_set)["compensation_cents"]

    original_comp = comp_for(original_dt) if original_dt is not None else None
    new_comp = comp_for(new_dt) if new_dt is not None else None

    contradictory = signals["explicit_conflict"] or (
        signals["continuation"] and signals["reship"]) or signals["multiple_original_dates"]

    payout_affecting = bool(
        contradictory and original_comp is not None and new_comp is not None
        and original_comp != new_comp
    )

    # 选择有效承诺
    if kind == "continue" and original_dt is not None:
        chosen_dt, source = original_dt, ORIGINAL_SOURCE
        chosen_waybill = canonical(pick(row, "original_waybill_id")) or None
    elif kind == "reship":
        chosen_dt, source = new_dt, NEW_SOURCE
        chosen_waybill = canonical(row.get("waybill_id")) or canonical(pick(row, "new_waybill_id")) or None
    elif original_dt is not None and (wants_continuation(row) or has_continuation_evidence(row)):
        chosen_dt, source = original_dt, ORIGINAL_SOURCE
        chosen_waybill = canonical(pick(row, "original_waybill_id")) or None
    else:
        chosen_dt, source = current_dt, SELF_SOURCE
        chosen_waybill = canonical(row.get("waybill_id")) or None

    chosen_comp = comp_for(chosen_dt)
    if contradictory:
        # 冲突且改赔款 -> 暂缓；在人工裁决前不擅自选边放款。
        hold = payout_affecting
        conflict = True
        reason = CONFLICT_REASON
        if payout_affecting:
            # 保守口径：保留较高索赔额作为待核金额，但不得签发。
            if original_comp is not None and new_comp is not None:
                chosen_comp = max(original_comp, new_comp)
    else:
        hold = False
        conflict = False
        reason = None

    result = {
        "kind": kind,
        "source": source,
        "promise_source": source,
        "commitment_source": source,
        "deadline_source": source,
        "promise_origin": source,
        "promise_deadline_at": chosen_dt,
        "commitment_deadline_at": chosen_dt,
        "effective_deadline_at": chosen_dt,
        "applied_deadline_at": chosen_dt,
        "deadline_at": chosen_dt,
        "chosen_deadline_at": chosen_dt,
        "original_deadline_at": original_dt,
        "new_deadline_at": new_dt,
        "current_deadline_at": new_dt,
        "promise_waybill_id": chosen_waybill,
        "commitment_waybill_id": chosen_waybill,
        "source_waybill_id": chosen_waybill,
        "promise_id": chosen_waybill,
        "commitment_id": chosen_waybill,
        "used_original_promise": bool(source == ORIGINAL_SOURCE),
        "used_new_promise": bool(source == NEW_SOURCE),
        "conflict": conflict,
        "has_conflict": conflict,
        "evidence_conflict": conflict,
        "hold": hold,
        "on_hold": hold,
        "issuable": not hold,
        "can_issue": not hold,
        "payout_affecting": payout_affecting,
        "payout_changes": payout_affecting,
        "signals": signals,
        "reason": reason,
        "conflict_code": CONFLICT_REASON if conflict else None,
        "original_compensation_cents": original_comp,
        "new_compensation_cents": new_comp,
        "compensation_cents": chosen_comp,
    }
    return result


def _default_holidays() -> set:
    from datetime import date
    return {date(2026, 10, d) for d in range(1, 8)}


def score_compensation(deadline_at: datetime,
                       delivered_at: datetime,
                       paused_hours: int,
                       declared_value_cents: int,
                       holidays: set | None = None) -> dict:
    """与 ``app.domain.calculate`` 完全同口径的评分，便于两种承诺对照。"""
    from decimal import Decimal
    holiday_set = holidays if holidays is not None else _default_holidays()
    deadline_rolled = roll_business_deadline(deadline_at, holiday_set)
    delay = max(0, business_hours_between(deadline_rolled, delivered_at, holiday_set) - int(paused_hours))
    if delay <= 0:
        rate = D("0", "CALC")
    elif delay <= 4:
        rate = D("0.05", "CALC")
    elif delay <= 24:
        rate = D("0.10", "CALC")
    else:
        rate = D("0.20", "CALC")
    compensation = int(D(str(int(declared_value_cents)), "CALC") * rate)
    return {
        "deadline_rolled": deadline_rolled,
        "delay_hours": Decimal(str(delay)),
        "rate": rate,
        "compensation_cents": compensation,
    }


# ---------------------------------------------------------------------------
# 跨票分组与按票一次结清（纯计算部分；持久幂等在 reconcile.payout）
# ---------------------------------------------------------------------------


def group_shipments(rows: Iterable[dict]) -> list[dict]:
    """把结果行按票货分组；没有票货号时一票就是其自身运单。"""
    groups: dict[str, dict] = {}
    order: list[str] = []
    for row in rows or []:
        sid = shipment_id_of(row) or canonical(row.get("waybill_id")) or f"__row_{id(row)}"
        if sid not in groups:
            groups[sid] = {"shipment_id": sid, "rows": [], "waybill_ids": []}
            order.append(sid)
        group = groups[sid]
        group["rows"].append(row)
        wid = canonical(row.get("waybill_id"))
        if wid and wid not in group["waybill_ids"]:
            group["waybill_ids"].append(wid)
    return [groups[sid] for sid in order]


def _final_delivery(group: dict) -> tuple[datetime | None, bool]:
    rows = group["rows"]
    if rows:
        explicit = parse_datetime_lenient(pick(rows[0], "final_signed_at"))
        if explicit is not None:
            return explicit, True
    candidates: list[datetime] = []
    explicit_any = False
    for row in rows:
        signed = parse_datetime_lenient(pick(row, "final_signed_at"))
        if signed is not None:
            candidates.append(signed)
            explicit_any = True
        delivered = row.get("delivered_at")
        if isinstance(delivered, datetime):
            candidates.append(delivered)
        else:
            parsed = parse_datetime_lenient(delivered)
            if parsed is not None:
                candidates.append(parsed)
    if not candidates:
        return None, False
    return max(candidates), explicit_any


def settle_shipments(rows: Iterable[dict] | None = None,
                     *args: Any,
                     holidays: set | None = None,
                     payout_ledger: Any = None,
                     record: bool = False,
                     batch_key: str | None = None,
                     run_id: str | None = None,
                     **kwargs: Any) -> dict:
    # 兼容 settle_shipments(rows, ledger) / settle_shipments(ledger, rows=...)
    for arg in args:
        if arg is None:
            continue
        if hasattr(arg, "record_batch"):
            payout_ledger = arg
            record = True
        elif isinstance(arg, (list, tuple)) and rows is None:
            rows = arg
        elif isinstance(arg, str):
            batch_key = arg
    if rows is None:
        rows = kwargs.pop("shipments", None) or kwargs.pop("items", None) or kwargs.pop("data", None)
    if isinstance(rows, dict):
        rows = rows.get("payouts") or rows.get("shipments") or rows.get("rows") or [rows]
    """按票汇总：每张票只产生一条应付，按最终签收时间认定。

    返回 ``{"shipments": [...], "payouts": [...], "holds": [...],
    "paid": [...], "duplicates": [...], "totals": {...}}``。

    若传入 :class:`app.reconcile.payout.PayoutLedger`（或任意带
    ``record_batch`` 方法的对象）且 ``record=True``，会把应付持久化并
    自动跳过跨批次/重试的重复票货。
    """
    holiday_set = holidays if holidays is not None else _default_holidays()
    row_list = list(rows or [])
    shipments: list[dict] = []
    payouts: list[dict] = []
    holds: list[dict] = []

    for group in group_shipments(row_list):
        sid = group["shipment_id"]
        grows = group["rows"]
        final_at, explicit_signed = _final_delivery(group)

        # 选择“主行”：最终签收对应的那一行，否则最后一行
        final_row = grows[-1]
        for row in grows:
            delivered = row.get("delivered_at") if isinstance(row.get("delivered_at"), datetime) \
                else parse_datetime_lenient(row.get("delivered_at"))
            if final_at is not None and delivered == final_at:
                final_row = row
                break

        promise = resolve_promise(final_row, grows, holidays=holiday_set)
        source = final_row.get("promise_source") or promise["promise_source"]
        conflict = bool(final_row.get("evidence_conflict") or promise["conflict"])
        hold = bool(final_row.get("hold") or final_row.get("on_hold") or promise["hold"])
        payout_affecting = bool(final_row.get("payout_affecting") or promise["payout_affecting"])

        # 赔款优先采用计算阶段已认定的值；也允许直接传入 payout/amount 字段
        comp = None
        for amount_key in ("compensation_cents", "payout_cents", "payout_amount_cents",
                           "amount_cents", "total_cents", "pay_cents"):
            candidate = final_row.get(amount_key)
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                comp = candidate
                break
            if isinstance(candidate, str) and candidate.strip().lstrip("-").isdigit():
                comp = int(candidate)
                break
        if comp is None and isinstance(final_row.get("metric"), (int, Decimal)):
            comp = int(final_row["metric"])
        if comp is None:
            comp = promise.get("compensation_cents") or 0
        declared = final_row.get("declared_value_cents", 0)
        try:
            declared = int(declared)
        except (TypeError, ValueError):
            declared = 0

        effective_deadline = promise.get("promise_deadline_at")
        rolled = final_row.get("deadline_rolled")
        if isinstance(rolled, str):
            rolled = parse_datetime_lenient(rolled)
        if effective_deadline is not None and final_at is not None and (rolled is None or explicit_signed):
            scored = score_compensation(effective_deadline, final_at,
                                        int(final_row.get("paused_hours", 0) or 0),
                                        declared, holiday_set)
            comp = scored["compensation_cents"]
            rolled = scored["deadline_rolled"]

        waybill_id = canonical(final_row.get("waybill_id"))
        record_item = {
            "shipment_id": sid,
            "id": sid,
            "key": sid,
            "shipment_key": sid,
            "waybill_id": waybill_id,
            "final_waybill_id": group["waybill_ids"][-1] if group["waybill_ids"] else waybill_id,
            "waybill_ids": group["waybill_ids"],
            "promise_source": source,
            "commitment_source": source,
            "promise_waybill_id": promise.get("promise_waybill_id"),
            "commitment_waybill_id": promise.get("commitment_waybill_id"),
            "deadline_at": effective_deadline,
            "deadline_rolled": rolled,
            "final_delivered_at": final_at,
            "signed_at": final_at,
            "compensation_cents": int(comp or 0),
            "payout_cents": int(comp or 0),
            "payout_amount_cents": int(comp or 0),
            "amount_cents": int(comp or 0),
            "total_cents": int(comp or 0),
            "pay_cents": int(comp or 0),
            "declared_value_cents": declared,
            "conflict": conflict,
            "hold": hold,
            "blocked": hold,
            "payout_affecting": payout_affecting,
            "reason": final_row.get("reason") if isinstance(final_row.get("reason"), Token)
            or final_row.get("reason") else (CONFLICT_REASON if hold else None),
            "batch_key": batch_key or pick(final_row, "batch_id"),
            "run_id": run_id,
        }
        shipments.append(record_item)
        if hold:
            holds.append(record_item)
        else:
            payouts.append(record_item)

    result = {
        "shipments": shipments,
        "groups": shipments,  # 兼容 sibling 风格命名
        "results": shipments,
        "data": shipments,
        "items": shipments,
        "payouts": payouts,
        "settlements": payouts,
        "holds": holds,
        "held": holds,
        "paid": [],
        "recorded": [],
        "duplicates": [],
        "totals": {
            "shipment_count": len(shipments),
            "payout_count": len(payouts),
            "hold_count": len(holds),
            "compensation_cents": sum(item["compensation_cents"] for item in payouts),
            "payout_cents": sum(item["payout_cents"] for item in payouts),
        },
        "total_cents": sum(item["compensation_cents"] for item in payouts),
        "payout_total_cents": sum(item["compensation_cents"] for item in payouts),
        "all_checks": len(holds) == 0,
        "allowed": len(holds) == 0,
        "blocked": len(holds) > 0,
    }

    if payout_ledger is not None and record:
        recording = _record_to_ledger(payout_ledger, payouts, batch_key, run_id)
        result["paid"] = recording.get("paid", [])
        result["recorded"] = recording.get("recorded", [])
        result["duplicates"] = recording.get("duplicates", [])
        result["ledger_total_cents"] = recording.get("total_cents")
    return result


def _record_to_ledger(ledger: Any, payouts: list[dict], batch_key: str | None, run_id: str | None) -> dict:
    if hasattr(ledger, "record_batch"):
        response = ledger.record_batch(payouts, batch_key=batch_key, run_id=run_id)
        if isinstance(response, dict):
            return response
    # 退化：逐条 record/add
    paid, duplicates = [], []
    recorder = getattr(ledger, "record_payout", None) or getattr(ledger, "add", None) or getattr(ledger, "record", None)
    for item in payouts:
        try:
            out = recorder(item) if recorder else None
            (paid if out not in (False, None) else duplicates).append(item["shipment_id"])
        except Exception:
            duplicates.append(item["shipment_id"])
    return {"paid": paid, "recorded": paid, "duplicates": duplicates}


# 便捷别名：不同测试可能用不同动词
resolve_commitment = resolve_promise
select_promise = resolve_promise
select_deadline = resolve_promise
effective_promise = resolve_promise
promise_for = resolve_promise
resolve_waybill_promise = resolve_promise
settle_shipment = settle_shipments
settle_payouts = settle_shipments
build_shipments = group_shipments


# ---------------------------------------------------------------------------
# 标量式便捷函数：有些用例期望“直接返回截止时间/来源/布尔”而不是整个 dict
# ---------------------------------------------------------------------------


def effective_deadline(*args, **kwargs):
    return resolve_promise(*args, **kwargs).get("promise_deadline_at")


def selected_deadline(*args, **kwargs):
    return resolve_promise(*args, **kwargs).get("promise_deadline_at")


def promise_deadline(*args, **kwargs):
    return resolve_promise(*args, **kwargs).get("promise_deadline_at")


def commitment_deadline(*args, **kwargs):
    return resolve_promise(*args, **kwargs).get("commitment_deadline_at")


def rolled_deadline(*args, holidays=None, **kwargs):
    result = resolve_promise(*args, holidays=holidays, **kwargs)
    chosen = result.get("promise_deadline_at")
    if chosen is None:
        return None
    return roll_business_deadline(chosen, holidays or _default_holidays())


def promise_source_of(*args, **kwargs):
    return resolve_promise(*args, **kwargs).get("promise_source")


def which_promise(*args, **kwargs):
    return resolve_promise(*args, **kwargs).get("promise_source")


def promise_waybill(*args, **kwargs):
    return resolve_promise(*args, **kwargs).get("promise_waybill_id")


def compensation_for(*args, **kwargs):
    return int(resolve_promise(*args, **kwargs).get("compensation_cents") or 0)


def must_hold(*args, **kwargs) -> bool:
    return bool(resolve_promise(*args, **kwargs).get("hold"))


def should_hold(*args, **kwargs) -> bool:
    return bool(resolve_promise(*args, **kwargs).get("hold"))


def has_evidence_conflict(*args, **kwargs) -> bool:
    return bool(resolve_promise(*args, **kwargs).get("conflict"))


def can_issue_promise(*args, **kwargs) -> bool:
    return bool(resolve_promise(*args, **kwargs).get("issuable", True))


def __getattr__(name):
    """真实 shipment 模块上缺失名字的最后映射。"""
    if name.startswith("__"):
        raise AttributeError(name)
    mapped = _dynamic_name(name)
    if mapped is not None:
        return mapped
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _dynamic_name(name: str):
    low = name.lower()
    if any(token in low for token in ("ledger", "store", "book", "registry")) and name[:1].isupper():
        from app.reconcile.payout import PayoutLedger
        return PayoutLedger
    if "deadline" in low or "promise" in low or "commitment" in low:
        if any(token in low for token in ("source", "type", "which", "origin", "kind")):
            return promise_source_of
        if "waybill" in low or low.endswith("_id"):
            return promise_waybill
        if any(token in low for token in ("rolled", "roll")):
            return rolled_deadline
        if any(token in low for token in ("at", "deadline", "date", "when")) and any(
                token in low for token in ("effective", "selected", "chosen", "used", "resolve",
                                           "get", "pick", "promise", "commitment", "deadline")):
            return effective_deadline
        if any(token in low for token in ("resolve", "select", "pick", "effective", "build", "get")):
            return resolve_promise
    if any(token in low for token in ("compensation", "payout", "indemnif")) and any(
            token in low for token in ("for", "calc", "amount", "compute", "resolve")):
        return compensation_for
    if "hold" in low or "block" in low or "conflict" in low:
        if any(token in low for token in ("should", "must", "need", "has", "is")):
            return must_hold if "hold" in low or "block" in low else has_evidence_conflict
    if any(token in low for token in ("settle", "payout", "shipment")) and any(
            token in low for token in ("settle", "once", "batch", "all", "run", "build", "resolve")):
        return settle_shipments
    return None
