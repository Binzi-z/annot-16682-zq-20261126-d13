# 离线核对证据报告

所有时间均为 UTC；区间按半开 `[start, end)` 计算。授权输入仅含虚构账号、当班区间和委托事件。

## 总览

- 枚举分钟数：240（2026-11-26T22:00:00Z 至 2026-11-27T02:00:00Z，右端点不枚举）
- 有效角色填充行数：2310，见 `evidence/minute_roles.csv`
- 输入 SHA-256：`a67b7146db4236a17928596c93790cecb009c190d9697290c81a7f030b6b940f`
- 端点检查：52/52 通过
- 席位端点检查：7/7 通过

## 批准/拒绝结论

| 用例 | 结论 | 预期 | 原因 | 共享实际控制人 |
|---|---|---|---|---|
| `C1_CROSSDAY_DUAL_DUTY_SHARED_CONTROLLER` | **APPROVED** | APPROVED | — | ctrl.harbor-zhou |
| `C2_START_ENDPOINT_IS_INCLUSIVE` | **APPROVED** | APPROVED | — | ctrl.harbor-zhou |
| `C3_END_ENDPOINT_IS_EXCLUSIVE` | **REJECTED** | REJECTED | INTAKE_CLAIMED_ACTOR_NOT_EFFECTIVE | — |
| `C4_SOURCELESS_RING_CANNOT_FILL_SEAT` | **REJECTED** | REJECTED | INTAKE_NO_EFFECTIVE_HOLDER, APPROVAL_NO_EFFECTIVE_HOLDER | — |
| `C5_ROOTED_MUTUAL_BATCH_APPROVE` | **APPROVED** | APPROVED | — | ctrl.mutual-root |
| `C6_OLD_APPROVAL_LATE_EXACT_HANDOVER` | **REJECTED** | REJECTED | APPROVAL_CLAIMED_ACTOR_NOT_EFFECTIVE | — |
| `C7_OLD_APPROVAL_LATE_AFTER_HANDOVER` | **REJECTED** | REJECTED | APPROVAL_CLAIMED_ACTOR_NOT_EFFECTIVE | — |
| `C8_NEW_HANDOVER_APPROVER_CAN_APPROVE` | **APPROVED** | APPROVED | — | — |

## 关键覆盖

- 跨日批准：`2026-11-27T00:07Z` 仍在跨日班次内；受理人为 `acct.harbor.lin`，批准人为 `acct.harbor.shen`。
- 批准席位存在 2 条简单路径，均收敛到根账号 acct.harbor.controller，实际控制人 ctrl.harbor-zhou。
- 半开端点：开始分钟包含，结束分钟排除；C2/C3 与 O1–O7 可重算。
- 无原始授权环：`org.ring` 的 A→B→C→A 不进入最小不动点，不产生席位。
- 同批互相依赖：`org.mutual` 的 X↔Y 与根授权同批求值；根→X 后，X、Y 均获得席位。
- 迟到旧审批：默认按审批到达引用监控器的时间判定；证据同时重算行为时间锚点。
  - 行为时间策略会得到 **APPROVED**；默认收件/引用监控时间策略得到 **REJECTED**。

## 增量重算一致性

- 单次换班影响关系键：`org.increment|approval|acct.inc.night-approver`, `org.increment|approval|acct.inc.night-root`
- 换班后实际重算组织：`org.increment`
- 未重算组织（4 个）：`org.harbor`, `org.late`, `org.mutual`, `org.ring`
- 增量维护计数：2430；全量重算计数：2430；一致：True
- 逐分钟席位状态完全一致：True；状态哈希一致：True

## 独立重算

```bat
py -3.12 duty_audit.py
py -3.12 verify_evidence.py evidence\evidence.json
py -3.12 -m unittest discover -s tests -v
```

`verify_evidence.py` 不导入本模块；它从证据中嵌入的当时有效事实独立执行最小闭包，并从原始输入重算增量计数。
