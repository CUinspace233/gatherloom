# 评测集

用于 S0 选型与 S4/S5 验收。真实材料不提交到公开仓库时，只保留结构与题目 ID。

## 账号与项目
| 账号 | 项目 | 用途 |
|---|---|---|
| test-a | 项目 A1：开发个人知识库 App | 主演示项目 |
| test-a | 项目 A2：（另一主题） | 验证项目范围隔离 |
| test-b | 项目 B1 | 验证账号隔离 |

## 材料（每项目 15–20 条，合计 ≥ 30）
需覆盖：同名技术、重复 URL、不同个人备注、截图、矛盾观点、决定变化；中文与中英混合。

| 字段 | 说明 |
|---|---|
| id | 材料编号 |
| project | 所属项目 |
| type | thought / url / image |
| source | URL 或文件名 |
| note | 个人备注 |

## 题目
- `retrieval.yaml`：20 道检索题，每题标注目标材料 id；指标：Top 5 命中（目标 ≥ 16/20）。
- `no_answer.yaml`：5 道无答案题；指标：不编造决定或来源。
- `scope.yaml`：跨项目、跨账号边界题。

## 文件与命令
当前的 `materials.yaml`（36 条）与题目由开发助手编写，用于打通流程和回归，不是真实材料。换成真实材料时保持相同字段即可。

```sh
cd server
uv run --locked --env-file .env python -m evaluation.load       # 导入并整理（eval_a、eval_b 账号）
uv run --locked --env-file .env python -m evaluation.retrieval  # 检索评测，写入 results/<日期>-retrieval.md
uv run --locked --env-file .env python -m evaluation.answers    # 问答评测（无答案题、范围题），写入 results/<日期>-answers.md
uv run --locked --env-file .env python -m evaluation.understanding  # 总结、关联建议、决定草稿与回顾，写入 results/<日期>-understanding.md
```

截图材料只有 OCR 文字，附件记录没有图片文件。

## 结果记录
每次评测记录：日期、Embedding 与分词版本、分块参数、上下文头与卡片 prompt 版本、模型路由、各指标结果、失败案例。

## 检索消融（ADR 0009）
| 配置 | 内容 |
|---|---|
| base | 原文 chunk，关键词 + 语义 RRF |
| +R1 | chunk 索引文本拼接资料标题、章节标题、备注摘录 |
| +R1+R2 | 加卡片路（标题、摘要、要点、关键词），按资料聚合 |
| +R1+R2+R3 | 卡片路加入“可回答的问题” |

每个配置记录 Top 5 命中、MRR、无答案题结果；按材料类型（想法 / 网页 / 截图）分列。某项增强使 Top 5 命中提升少于 1 题且增加成本时关闭。引用支持度、回顾质量、延迟分开记录，不合并为单一"准确率"。
