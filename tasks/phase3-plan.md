# Phase 3：标准化 ModelProvider 抽象层

## 目标

在不改变认证、异步批次、既有 Schema、页面和确定性写库门的前提下，把模型供应商选择从 `CodexNotebookModel` 和启动器中抽离，形成可显式选择的 DeepSeek 与阿里百炼 Qwen-VL 双通道。

## 冻结边界

- `NotebookAgent` 继续拥有识题、独立解题、AST 验算、判题、连续会话和候选校验；Provider 只能生成不可信候选，不能推进批次、写库、认证、授权或决定是否入本。
- `ModelProvider` 只暴露结构化生成、连续会话、历史读取、压缩、能力声明和稳定错误语义；API、摄取管线和领域服务不得按供应商分支。
- `MODEL_PROVIDER=deepseek|dashscope` 是业务供应商选择。现有 `HARNESS_PROVIDER` 保留为 Harness 内部路由 ID，默认仍为 `notebook-provider`，不复用一个变量表达两种含义。
- DeepSeek 与 DashScope 首版均复用固定 Harness 运行时，以保留已实现的附件归一化、持续会话、事件、历史和压缩能力；应用自有 `ConversationStore` 属于后续迁移阶段，不在本阶段复制一套会话系统。
- DashScope 只接受 HTTPS 的阿里云百炼兼容端点；业务空间专属 `*.maas.aliyuncs.com/compatible-mode/v1` 为推荐入口，兼容旧 `dashscope.aliyuncs.com/compatible-mode/v1`。端点来自部署配置，不接受浏览器输入。
- API Key 仅通过配置中声明的环境变量名间接读取；对象、日志、异常、测试夹具和 Git 中均不保存密钥值。
- 不自动跨供应商回退。认证、权限、参数、Schema、内容安全和配置错误失败闭合；网络、超时、限流和服务不可用仍由现有 Harness 有界重试，最终只暴露稳定公开错误码。
- 模型输出继续由现有 JSON Schema、资源 ID、输入版本、长度上限和领域质量门校验。AST 验算作为供应商无关的确定性过滤器，在独立解答冻结后、判题候选生成前运行。
- 保持 `CodexNotebookModel` 兼容导出，新增供应商无关名称 `NotebookAgent`；现有调用方和测试注入回调不强制迁移。
- 不接触注册状态机、数据库迁移、存储抽象、生产部署、OSS、前端或第五阶段学情功能。

## 实现切片

1. 先写 Provider 契约、配置拒绝和委托行为测试，确认当前代码尚未满足契约。
2. 新增 `services/web_domain/model_provider/`：能力、稳定错误、运行配置、DeepSeek/DashScope Provider 和工厂。
3. 让 Harness 运行时接受显式且不含密钥值的子进程配置；让 `NotebookAgent` 依赖 Provider，同时保留旧回调注入兼容面。
4. 把 AST 验算挂成可注入的确定性 `MathVerificationFilter`，维持原有判题输入与输出。
5. 更新本地启动装配、配置模板和架构状态；运行聚焦单元测试、Python/JSON/YAML/差异语法检查并提交。

## 验收

- 默认不设置 `MODEL_PROVIDER` 时继续使用 DeepSeek 现有链路。
- `MODEL_PROVIDER=dashscope` 时只从 DashScope 专用环境变量装配 Qwen-VL 通道，配置不完整或端点不安全时启动失败闭合。
- 两个 Provider 对应用暴露相同方法和能力结构，返回既有业务候选形状；API 和 `NotebookIntakeBatchProcessor` 不出现供应商判断。
- AST 验算结果不依赖 Provider，冲突或不支持表达式的既有语义不变。
- 不新增第三方依赖，不记录密钥、题目、图片或提示词。
- 按用户要求不运行全量测试，只运行新增与直接相关测试及语法检查；报告中如实标记未做真实百炼联网评测。

## 任务卡

- `design_gate`: T1，冻结 Provider 契约、失败策略、秘密边界和兼容面
- `implementation_tier`: T2，Provider 装配、运行时配置和 Agent 解耦
- `model`: 本地实现；最终安全语义按项目 `web-security-review` 路由复核
- `effort`: 默认
- `escalation_reason`: 无
- 必读：`PROJECT_ARCHITECTURE.md`、`docs/18-MODEL-PROVIDER-MIGRATION.md`、`docs/24-PRODUCT-ARCHITECTURE-REFACTORING-AND-PLAN.md`、现有模型运行时、Agent、启动器和相关测试
- 禁止：外发真实题目/图片/身份数据，写入任何密钥，自动供应商回退，修改认证/数据库/存储/部署边界
