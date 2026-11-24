def approval_allowed(payload):
    """存在 REJECT 计数，或存在会改变赔款的换单凭据冲突/暂缓项时禁止签发。"""
    if int(payload.get("summary", {}).get("REJECT", 0)) != 0:
        return False
    gate = payload.get("gate") or {}
    if gate.get("blocked") or gate.get("approval_allowed") is False:
        return False
    if int(payload.get("hold_count", 0) or 0) > 0:
        return False
    if payload.get("holds") or payload.get("conflicts") or payload.get("blocked"):
        return False
    # 对账台账里的暂缓票
    ledger = payload.get("ledger") or {}
    if ledger.get("holds") or ledger.get("blocked"):
        return False
    for shipment in ledger.get("shipments", []) or []:
        if isinstance(shipment, dict) and (shipment.get("hold") or shipment.get("on_hold")):
            return False
    # 兜底：直接扫描结果行
    for row in payload.get("rows", []) or []:
        if isinstance(row, dict) and (row.get("hold") or row.get("on_hold")) and (
                row.get("evidence_conflict") or row.get("payout_affecting")):
            return False
    return True
