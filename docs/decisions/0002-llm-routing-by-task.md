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
- [ ] 列出两家当前可用模型 ID（不依赖别名）。
- [ ] 每档用 10 条样本比较 JSON 合法率、字段正确率、中文质量、延迟与成本。
- [ ] 确定每账号每日预算与默认路由。
