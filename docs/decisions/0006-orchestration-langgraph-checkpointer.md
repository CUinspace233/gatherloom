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
- [x] Worker 在节点间崩溃后从检查点恢复，发布结果不重复。自动化测试覆盖节点间中断、节点事务中途中断、失去租约的旧 Worker 写入；本机 `kill -9` 实测在卡片步骤中断后续做，未重做 Embedding。崩溃发生在模型调用返回后、写入前时，重启会再调用一次模型（runs 表可见），符合 product-plan §10.1“不承诺零重复计费”。
- 实施要点：`invoke` 使用 `durability="sync"`（默认 `"async"` 在下一步执行时才写检查点）；`PostgresSaver.setup()` 用 `CREATE INDEX CONCURRENTLY`，不能放进 Alembic 事务，由 Worker 启动时执行；任务结束（成功、失败、取消）后删除该线程的检查点。
- [x] 删除资料后无法从旧检查点恢复出包含已删除正文的结果：图状态只含 ID，节点从领域表读取正文；测试断言检查点中不含正文。删除时任务改为 cancelled，持有租约的 Worker 写入被围栏拒绝；S6 的 `purge_checkpoints` 仍需实现。
