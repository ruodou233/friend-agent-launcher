# Payment control plane — V1A reference

状态：**本地可验证模板，未部署、未连接真实 New API、未包含 Key 或支付凭据**。

这里保存人工微信充值的申请、claim 和脱敏证据，不保存余额第二账本。余额仍只读 New API。`schema.sql` 要求 `request_id` 与 `business_ref` 各自 `NOT NULL UNIQUE`；服务端由 `business_ref = manual-wechat:<request_id>` 推导业务引用。

状态机只有：

```text
pending ──原子 claim──> crediting ──New API 已确认执行──> credited
                              │
                              ├─未知/超时/不一致──> crediting
                              ├─两份未执行且未扣款证据──> failed
                              └─两份未执行且未扣款证据──> pending ──最多再 claim 一次
```

`claim_recharge()` 使用 `BEGIN IMMEDIATE` 加条件更新。返回空 claim 表示抢占失败，调用方不得调用 New API。`run_claim()` 只有拿到 claim 才调用 provisioner；异常被记录为脱敏 `unknown`，不会猜测为 failed，也不会自动重试。

要进入 `failed` 或第二次 claim，必须已有同一 claim 的 `not_executed` 证据，且同时具备人工确认引用和 New API 未扣款引用。没有这些证据，状态保持 `crediting`。

本地验收：

```bash
bash verify.sh
```

该验证使用 Python 标准库 SQLite；生产接入 MySQL 时必须保留同样的唯一约束、事务边界、条件 claim、证据先写后转态和“余额不落控制面”语义。这里没有真实管理端、New API 客户端或公网部署证明。
