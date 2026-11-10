# 架构决策记录（ADR）

每条决策一个文件：背景 / 决策 / 后果 / 待验证。状态取值：已采纳、待验证、已替代。

| 编号 | 标题 | 状态 |
|---|---|---|
| [0001](0001-embedding-bailian-qwen.md) | Embedding 使用百炼 qwen3.7-text-embedding-flash | 已采纳，S0 待验证 |
| [0002](0002-llm-routing-by-task.md) | 文本生成按任务分流 | 已采纳，S0 选定模型 |
| [0003](0003-ocr-on-device-vision.md) | 截图 OCR 以 iOS 端侧 Vision 为主 | 已采纳 |
| [0004](0004-web-extraction-safari-preprocessing.md) | 网页正文由 Safari 分享扩展预处理获取 | 已采纳 |
| [0005](0005-chinese-keyword-search-jieba.md) | 中文关键词检索采用应用侧 jieba | 已采纳 |
| [0006](0006-orchestration-langgraph-checkpointer.md) | 保持 LangGraph + PostgreSQL checkpointer | 已采纳 |
| [0007](0007-deployment-overseas-server.md) | 服务器部署在海外/港澳 | 已采纳 |
| [0008](0008-apple-signing-free-team.md) | 暂用免费签名，内测前购买开发者账号 | 已采纳 |
| [0009](0009-knowledge-card-retrieval-augmentation.md) | 知识卡片作为派生检索单元，检索增强按评测逐项启用 | 已采纳，S0/S4 待验证 |
