# ADR 002：ModelProvider 供应商边界

- 状态：已接受
- 日期：2026-08-30
- 范围：v0.5.0 Phase 3

## 决策

应用新增供应商无关的 `ModelProvider` 契约，`NotebookAgent` 只依赖该契约，不依赖供应商 SDK、端点或模型名称。首版提供 `DeepSeekHarnessProvider` 与 `AliyunDashscopeProvider`，两者均复用当前固定版 Harness 运行时，以保留已经工作的图片归一化、结构化候选、持续会话、事件、历史和压缩能力。

`MODEL_PROVIDER` 只选择业务供应商，允许值固定为 `deepseek`、`dashscope`；`HARNESS_PROVIDER` 继续表示 Harness 内部路由 ID。两者不得合并，因为应用供应商身份和运行时内部路由不是同一状态。

Provider 只能读取明确配置并生成候选。认证、用户归属、文件哈希、输入版本、Schema、AST 验算、幂等、批次推进和正式写库继续由确定性应用代码裁决。

## 契约

每个 Provider 必须提供：

- 不可变能力声明；
- 结构化多模态生成；
- 连续会话生成；
- 历史分页和压缩；
- 稳定公开错误分类；
- 不包含正文、图片、提示词或密钥的供应商/模型元数据。

Provider 输出沿用当前 `route/result/thread_id` 封包，避免改变 `NotebookAgent`、ASGI、异步摄取和前端可观察契约。模型输出始终是不可信数据，仍须通过既有字段、资源 ID、版本、长度和 JSON Schema 校验。

## 配置与安全

- 默认 Provider 是 `deepseek`，保持现有行为。
- DashScope 使用部署环境中的 `DASHSCOPE_API_KEY`，对象与日志中只保存环境变量名。
- DashScope 端点必须使用 HTTPS，且主机只能是阿里云百炼兼容域名；路径必须以 `/compatible-mode/v1` 结束。
- 不接受浏览器提交端点、模型名或 API Key。
- 不实现自动跨供应商回退，避免静默改变数据处理主体、答案分布和失败语义。
- 网络、限流和服务不可用可沿用运行时已有的有界重试；认证、权限、参数、Schema、内容安全和配置错误不重试并安全失败。

## AST 验算

现有 `services/web_app/math_verifier.py` 保持唯一确定性实现。新增的 `MathVerificationFilter` 只负责把独立解答中的有界 `verification_checks` 交给该实现，并把结果作为判题复核证据；Provider 不得覆盖或伪造验证结果。

## 未选择的方案

- 本阶段不直接引入供应商 SDK，避免新增依赖和锁文件风险；Harness 已提供经验证的 OpenAI 兼容传输。
- 本阶段不建设第二套会话存储。应用自有 `ConversationStore`、供应商线程丢失恢复和完整灰度属于 `docs/18-MODEL-PROVIDER-MIGRATION.md` 后续阶段。
- 不让 API 或异步任务引擎按供应商分支，否则会形成平行 intake/判题实现。

## 官方协议依据

- 阿里云百炼 OpenAI 兼容 Chat：<https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions>
- 阿里云百炼结构化输出：<https://help.aliyun.com/zh/model-studio/qwen-structured-output>
- 阿里云百炼视觉理解：<https://help.aliyun.com/zh/model-studio/vision-model/>
- 阿里云百炼错误码：<https://help.aliyun.com/zh/model-studio/error-code>

当前官方协议确认：视觉输入使用 `messages[].content` 数组中的 `image_url`，支持 Base64 Data URL；复杂结构化输出可使用严格 `json_schema`；认证使用 Bearer API Key；429、5xx 和服务不可用属于可恢复类别，401/403 和请求参数错误应失败闭合。实际候选模型及其严格 Schema 能力仍须在部署当日用冻结样本复核。
