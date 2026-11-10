# 0002 文本生成按任务分流

- 日期：2026-11-09
- 状态：已采纳，模型 ID 在 S0 选定

## 背景
已有 DeepSeek API，同时已接入百炼（可用千问）。不同任务对质量与成本要求差异大：摘要、标题量大且简单；问答、总结、决定草稿要求高。

## 决策
定义统一的 `LLMProvider` 接口（chat、JSON 结构化输出、用量回传），由任务路由配置选择模型：

| 任务档位 | 任务 | 选择原则 |
|---|---|---|
| light | 自动标题、资料摘要、关联理由 | 低成本、低延迟 |
| heavy | 项目问答、项目总结、决定草稿、阶段回顾 | 质量优先，结构化输出稳定 |

DeepSeek 与千问均可作为任一档位的 Provider；配置示例：

```yaml
llm_routes:
  light: { provider: qwen, model: <S0 选定> }
  heavy: { provider: deepseek, model: <S0 选定> }
  fallback: { provider: qwen, model: <S0 选定> }
```

## 后果
- 每次运行记录 provider、model、prompt_version、用量，便于比较与切换。
- 提示词按档位维护版本；更换生成模型不需要重建向量。
- 可配置 fallback，但认证错误、超额不自动切换到其他付费 Provider，避免意外费用。

## 待验证（S0）
- [x] 列出两家当前可用模型 ID（不依赖别名）。2026-11-10：DeepSeek `/models` 只返回 `deepseek-flash`、`deepseek-v4-pro`；百炼中国站有带日期快照 `qwen3.7-flash-2026-07-15`。初始路由：light = `qwen3.7-flash-2026-07-15`，heavy = `deepseek-v4-pro`，不配置 fallback。
- 2026-11-10 更新：路由改为 light = `qwen3.8-flash`（¥0.8 / ¥2.7 每百万 token，不分档），heavy = `deepseek-flash`（高峰 $0.3 / $1.2）。`qwen3.8-flash` 没有带日期的快照，只能用别名；别名指向的模型变化时，以 `runs` 表的记录和 `check-models` 复测为准。两者都默认思考，分别用 `enable_thinking: false` 和 `thinking: {"type": "disabled"}` 关闭。
- 千问 3.x 默认开启思考模式：同一张小卡片开启时 12.5 s、1,046 个推理 token，关闭（`enable_thinking: false`）时 1.1 s，输出质量相当。light 档调用一律关闭思考。
- 价格（2026-11-10）：`qwen3.7-flash-2026-07-15` 输入 ≤32K 档 ¥0.2 / ¥0.8 每百万 token（输入 / 输出）；`deepseek-v4-pro` 高峰时段 $1.32 / $3.96，非高峰半价，费用估算按高峰价记录上限。
- [ ] 每档用 10 条样本比较 JSON 合法率、字段正确率、中文质量、延迟与成本。尚未做；S3 实测 5 张知识卡片（含 check-models）均首次即合法，单次调用 1.7–4.2 s，¥0.0002–0.0013（长网页节选约 5K 输入 token）。
- [ ] 确定每账号每日预算与默认路由。
