---
name: extend-one-night
description: Continue an existing paid stay for exactly one additional night while preserving the selected room, guest count, and meal choice.
---

# 续住一晚

当用户要求续住、续订或延长入住一晚时，使用此流程。续住会创建一笔**独立的待支付续住订单**，不会修改原订单；因此原订单的房源、入住人数和餐食选择保持不变。

## 流程

1. 先调用 `list_my_orders`，优先查询 `trade_state=1` 的待入住订单。
2. 若有多笔订单，要求用户用订单号或列表中的唯一序号选择要续住的订单；不能仅凭模糊的民宿名称猜测。
3. 从选中订单读取并复用：
   - `homestay_id`：同一房源；
   - `guest_count`：同一入住人数；
   - `need_food`：同一餐食选择；
   - `check_out`：续住订单的 `check_in`。
4. 将新的 `check_out` 计算为原 `check_out` 的后一天，必须使用 `YYYY-MM-DD`。只续住一晚，不要让用户再选择天数。
5. 调用 `get_homestay_detail` 获取当前房源与餐食价格，用于展示续住订单预览。价格以新订单实际创建结果为准。
6. 展示：原订单号、房源、续住日期、人数、餐食、预计一晚费用，并说明这是新的待支付续住订单。取得用户对“创建续住订单”的明确确认。
7. 确认后调用 `create_homestay_order`，参数名必须完全一致：

```json
{
  "homestay_id": 123,
  "check_in": "2026-08-24",
  "check_out": "2026-08-25",
  "guest_count": 2,
  "need_food": true,
  "remark": "续住来源订单：HSO...",
  "idempotency_key": "fresh-key-at-least-8-chars"
}
```

8. 使用本轮新生成的 `idempotency_key`；重试同一个请求时使用同一个 key。不要使用 `guests`、`check_in_date` 或 `check_out_date` 等不存在的字段。
9. 创建成功后，说明原订单未改变、续住订单为待支付。只有用户再次明确确认模拟支付时，才调用 `confirm_demo_payment`。

## 边界

- 原订单不是待入住状态、续住日期发生冲突或房源不可用时，如实说明，不能绕过校验。
- 用户要求“保持配置不变”只复用房源、人数和餐食；新订单价格按当前房源价格重新计算。
- 不要自动支付、不要取消原订单、不要承诺续住成功直到 `create_homestay_order` 返回成功。
