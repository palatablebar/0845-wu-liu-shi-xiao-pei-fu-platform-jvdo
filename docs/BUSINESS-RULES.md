# 业务规则

## 使用者

干线客服专员使用本工作台处理工作日顺延与赔付阶梯。

## 主规则

截止时点落在非工作日时顺延，计算延误时扣除暂停时长与非工作时段，再套赔付档。

计算以整数最小单位或十进制定点数为准，避免二进制浮点在边界上漂移。无法完成前置校验的行进入隔离区；跨行规则在计算后标记阻断或人工复核。

## 判定

- `PASS`：业务结果在允许范围内。
- `REVIEW`：数据可计算，但需要人工确认或存在轻微偏离。
- `REJECT`：存在来源、守恒、方向或上限阻断，不能签发。

## 批次

同一来源重复执行得到同一批次数值。签发要求阻断项为零，并携带当前版本号；旧版本提交会被拒绝。证据只在签发后生成。

## 换单续运与取消重发

同一票货（`shipment_id`，兼容 `cargo_id` / `ticket_id` / `order_id` / 中文“票货号”等写法）可能对应多张运单：

- **续运**：旧单已揽收（`picked_up=true` 或存在揽收时间/轨迹）且存在续运凭据（`continuation_evidence`），到货承诺沿用**原来的**承诺（`promise_source=ORIGINAL`）。只按新单更晚的截止计算会少赔。
- **取消重发**：旧单未揽收即取消（`cancelled=true` 且 `picked_up=false`，或显式 `reship=true`），承诺改按**新**单截止（`promise_source=NEW`）。沿用旧的较早承诺会多赔。
- **冲突暂缓**：续运与取消重发凭据相互矛盾、显式 `evidence_conflict=true`、或原承诺时间不一致，并且两种承诺算出的赔款不同（`payout_affecting=true`）时，该票 `hold=true`，判定为阻断/暂缓，`APPROVED` 签发返回 `409 GATE_BLOCKED`；赔款相同的轻微冲突只作复核提示。
- **跨批一次赔**：票货赔付持久化在 SQLite 台账 `shipment_payouts`（`shipment_id` 主键唯一）与批次分录 `shipment_postings`（`(batch_key, shipment_id)` 唯一）。同一票货跨批次只按**最终签收时间**（`final_delivered_at` / `signed_at` / 最大 `delivered_at`）赔付一次；签发重试、旧版本重试或重复签发都不会重复给付。
- **承诺可追溯**：结果行与 `ledger.promises` / `ledger.commitments` 记录 `promise_source`、`promise_waybill_id`、`promise_deadline_at` 及两种口径赔款；对账接口 `GET /api/reconcile/promise?shipment_id=...`（也支持 `waybill_id`）可查询实际采用的是哪一次承诺，`GET /api/payouts` 查询已赔台账。

核心实现在 `app/domain/shipment.py`，持久台账在 `app/reconcile/payout.py`（`PayoutLedger`，并以 `PayoutMixin` 混入复核 `Store`）。
