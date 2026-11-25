# 跨日值班角色复核

题面见 [`task/PROMPT.txt`](task/PROMPT.txt)。本目录是一个仅使用 Python 3.12
标准库的 Windows 离线核对器：授权输入只有虚构账号、UTC 当班区间和委托事件。

## 运行

原生 Windows：

```bat
run_checks.bat
```

或逐步执行：

```bat
py -3.12 duty_audit.py
py -3.12 verify_evidence.py evidence\evidence.json
py -3.12 -m unittest discover -s tests -v
```

`duty_audit.py` 会生成：

- `evidence/evidence.json`：每个批准/拒绝结论所需的当时有效班次、委托、最小闭包、证明链、控制人、端点检查和增量重算证据；
- `evidence/minute_roles.csv`：时间范围内每分钟、每个组织/职责的有效账号填充；
- `evidence/report.md`：中文结论摘要。

`verify_evidence.py` 不导入 `duty_audit.py`，会从证据内嵌事实独立重算闭包和证明链，
并从 `data/synthetic_duty.json` 再次全量计算增量计数与状态哈希。

## 建模规则

1. 所有时间为 UTC，分钟标签 `t` 表示半开区间 `[t, t+60s)`。
2. 班次是唯一的原始授权种子；委托边从 `(org, role, delegator)` 指向
   `(org, role, delegate)`。
3. 有效席位是从活动班次种子出发，经活动委托边可达的最小不动点。
4. 同一批换班中的相互委托作为同一组边一次性求闭包，因此不依赖声明顺序。
5. 没有任何原始班次可达的委托环不会进入闭包，不产生席位。
6. 受理 (`intake`) 和批准 (`approval`) 必须在决定分钟分别由声称账号有效担任。
   两条链收敛到同一实际控制人时在证据中标出；题面允许共享，因此不单独否决。
7. 默认按审批声明到达引用监控器的时间判断；对于“行为时有效、换班后才迟到”的
   旧审批，证据同时给出按行为时间锚定的替代结论，便于审计重放。

## 已覆盖场景

- 跨日 `2026-11-26 → 2026-11-27` 的双职责批准；
- `[start, end)` 的起点包含、终点排除，以及终点分钟的拒绝用例；
- 批准席位经 Wu 和 Zheng 两条路径收敛到同一根账号/实际控制人；
- `org.ring` 的无原始授权 A→B→C→A 环不产生席位；
- `org.mutual` 同批 X↔Y 互相依赖，但由根班次→X 接入后可产生席位；
- 旧批准人在 `00:59Z` 有效、`01:00Z` 换班端点后迟到，按到达时间拒绝；
- 新班次批准人在换班后可正常批准；
- 对 `org.increment` 只加入一个夜班批准班次种子，依赖闭包仅含
  `acct.inc.night-root → acct.inc.night-approver`，只重算 `org.increment`；
  增量维护计数、逐分钟席位状态和 SHA-256 均与全量重算一致。
