# 字段定义

| 字段 | 类型 | 说明 |
|---|---|---|
| `waybill_id` | text | 主键 |
| `pickup_at` | datetime | 业务字段 |
| `deadline_at` | datetime | 业务字段 |
| `delivered_at` | datetime | 业务字段 |
| `paused_hours` | int | 业务字段 |
| `declared_value_cents` | int | 业务字段 |
| `invoice_cents` | int | 业务字段 |

空字符串、`-`、`NA`、`null` 和 `未提供` 按空值处理。金额字段在整理层转为整数分或整数最小单位；日期按 ISO 日期解析；带时间的字段按本地时间解析。标识字段保持为文本，前导零不会被去除。
