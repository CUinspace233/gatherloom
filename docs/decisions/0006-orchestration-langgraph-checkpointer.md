# 0006 保持 LangGraph + PostgreSQL checkpointer

- 日期：2026-11-09
- 状态：已采纳

## 背景
评估中提出入库、维护流程多为确定性流水线，checkpointer 是第三套状态且增加删除清理成本。用户确认保持原方案。

## 决策
- 三条图（EntryIngestionGraph、ProjectMaintenanceGraph、ProjectAnswerGraph）使用 LangGraph，启用 PostgreSQL checkpointer。
- 职责边界不变：领域表是业务事实；jobs 表决定待执行任务与租约；checkpointer 只保存单次运行的执行位置。对外状态从领域任务状态投影。
- 每个 run 的 thread_id 携带 owner_id 与 run_id，便于按账号、按资料定位检查点。

## 后果
- 删除资料时，删除流程登记 `purge_checkpoints` 任务：清理引用该 entry/revision 的运行检查点，并把这些运行标记为不可恢复发布。
- 账号删除时按 owner 清理全部检查点。
- 图节点写入必须幂等，以支持重放。
- 锁定 LangGraph 与 checkpointer 包版本，升级前验证检查点兼容性。

## 待验证（S3）
- [ ] Worker 在节点间崩溃后从检查点恢复，发布结果不重复。
- [ ] 删除资料后无法从旧检查点恢复出包含已删除正文的结果。
