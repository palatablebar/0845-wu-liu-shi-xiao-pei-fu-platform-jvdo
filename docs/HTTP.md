# 接口

- `GET /`：工作台。
- `GET /api/catalog`：阶段、字段和样例。
- `POST /api/stages/{stage}`：按阶段返回处理结果。
- `POST /api/runs`：执行完整批次并保存草稿。
- `GET /api/runs/{id}`：读取批次。
- `POST /api/runs/{id}/review`：签发或暂缓，提交 `decision`、`comment`、`expected_revision`。
- `GET /api/runs/{id}/evidence`：下载已签发证据。
- `GET /api/examples/{profile}`：下载样例。
- `GET /api/reconcile`：对账总览与已赔台账。
- `GET /api/reconcile/promise?shipment_id=...|waybill_id=...`：查询该票/该单实际采用的是哪一次到货承诺（草稿批次与已签发台账均可查）。
- `GET /api/payouts?shipment_id=...`：票货赔付台账（跨批去重、重试不重复给付）。
- `POST /api/shipments/settle`：对提交的 `rows` 直接做按票结清试算。
- `POST /api/runs`、`POST /api/stages/{stage}` 除 `profile` 外也接受 JSON `rows`（dict 列表，可携带 `shipment_id`、`original_deadline_at`、`picked_up`、`cancelled`、`continuation_evidence`、`reship`、`evidence_conflict` 等扩展字段）或 `csv` 文本。
- `GET /healthz`：健康检查。
