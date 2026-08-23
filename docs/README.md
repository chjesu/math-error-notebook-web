# 李兆霖数学错题本 Web 版文档索引

> 文档基线：v0.4.0
> 基线日期：2026-08-23
> 当前状态：v0.4.0 本地候选已实现；OpenAPI 为 30 paths，134 项测试通过，真实 MySQL smoke、独立安全复核与浏览器验收已完成。生产门禁未通过。

## 当前产品口径

1. 已有账号使用手机号验证码登录；新用户在独立注册页使用手机号、验证码和密码注册。
2. 注册需确认用户协议和隐私政策，成功后自动登录并进入个人错题本。
3. 不填写姓名、昵称、年级、年龄、身份或其他个人资料。
4. 不做家庭、成员、学生档案、监护、实名或普通用户角色。
5. 一个账号对应一个私有错题本，所有数据以服务端 user_id 隔离。
6. 上传、判题、错因和推荐先作为候选，确认和质量门后才成为正式记录。
7. 推荐只使用已验证且授权允许的题。
8. 本地完整手工学习闭环已验收；云部署继续后置，必须完成独立安全复核和全部生产门禁。

## 文档导航

| 文档 | 用途 | 版本 | 状态 |
|---|---|---:|---|
| [01-DISCUSSION-MINUTES.md](./01-DISCUSSION-MINUTES.md) | 当前决策、范围和被替代决策 | v0.4.0 | 已确认 |
| [02-PRODUCT-REQUIREMENTS.md](./02-PRODUCT-REQUIREMENTS.md) | 活跃需求、验收标准、指标和暂缓需求 | v0.4.0 | 已确认 |
| [03-TECHNICAL-ARCHITECTURE.md](./03-TECHNICAL-ARCHITECTURE.md) | 最小系统架构、状态、迁移与安全边界 | v0.4.0 | 已同步实现；生产门禁未完成 |
| [04-IMPLEMENTATION-PLAN.md](./04-IMPLEMENTATION-PLAN.md) | 分批实施、迁移和完成证据 | v0.4.0 | 本地候选已落地；生产批次待执行 |
| [05-TEST-ACCEPTANCE-OPERATIONS.md](./05-TEST-ACCEPTANCE-OPERATIONS.md) | 安全与学习闭环验收 | v0.4.0 | 已确认 |
| [06-WORKBREAKDOWN-CODEX-WORKFLOW.md](./06-WORKBREAKDOWN-CODEX-WORKFLOW.md) | 角色、任务领取、恢复与模型路由 | v0.4.0 | 待更新工作流实例 |
| [07-SMS-PROVIDER-RUICHENG.md](./07-SMS-PROVIDER-RUICHENG.md) | 短信适配、密钥和网络约束 | v0.2.0 | 待验收 |
| [08-IMPLEMENTATION-EVIDENCE.md](./08-IMPLEMENTATION-EVIDENCE.md) | 当前实现、测试、本地环境与剩余门禁 | v0.4.0 | 本地闭环已验收；生产门禁未完成 |
| [09-PRODUCT-FUNCTION-DESIGN.md](./09-PRODUCT-FUNCTION-DESIGN.md) | 页面、流程、异常恢复和产品事件 | v0.4.0 | 已确认 |
| [10-UX-UI-INTERACTION-DESIGN.md](./10-UX-UI-INTERACTION-DESIGN.md) | 信息架构、线框、视觉规范、交互状态和前端顺序 | v0.4.0 | 核心流程可评审 |
| [11-REMAINING-FUNCTION-WBS.md](./11-REMAINING-FUNCTION-WBS.md) | 剩余功能任务、依赖、并行线、验收证据和完成定义 | v0.4.0 | 可执行基线 |
| [12-ARCHITECTURE-DATA-API-CONTRACT.md](./12-ARCHITECTURE-DATA-API-CONTRACT.md) | 权限矩阵、user_id Schema、候选/正式记录、API 错误与迁移契约 | v0.4.0 | 已与 30 paths OpenAPI 同步 |
| [13-LOGIN-REGISTER-PRD.md](./13-LOGIN-REGISTER-PRD.md) | 登录/注册页面、字段、状态、风控、目标 API 与验收标准 | v0.4.0 | 本地实现已验收；生产安全门禁未完成 |
| [14-CODEX-MULTI-AGENT-TEAM.md](./14-CODEX-MULTI-AGENT-TEAM.md) | 多角色子智能体、岗位能力、并行工作流、模型路由和治理边界 | v1.0 | 已建立 |
| [`../openapi/web-v1.json`](../openapi/web-v1.json) | 当前完整本地候选机器可校验 API 契约 | v0.4.0 | 30 paths；生产部署前仍需契约复核 |

## 活跃需求

| 编号 | 主题 | MVP |
|---|---|---|
| ACCOUNT-001 | 独立注册、设置密码并自动登录 | 是 |
| AUTH-001、AUTH-002 | 验证码登录、注册验证和防轰炸 | 是 |
| INTAKE-001、INTAKE-002 | 上传、切分和确认 | 是 |
| GRADE-001、ERROR-001 | 判题、首错步骤和正式错题本 | 是 |
| CONTENT-001、RECOMMEND-001 | 题库验证和推荐质量门 | 是 |
| REVIEW-001、PDF-001 | 分阶段复习和练习 PDF | 是 |
| JOB-001、WORKBENCH-001 | 可恢复任务和工作台 | 是 |
| PRIV-001、PRIV-002、PRIV-003 | 敏感数据、导出和注销 | 是 |
| OPS-001 | 最小运营和人工复核 | 是 |

## 暂缓需求

以下稳定编号不删除，但不属于当前 MVP：

- AUTH-003：监护同意；
- TENANT-001、TENANT-002：家庭创建和加入；
- AUTHZ-001：成员与角色；
- STUDENT-001：独立学生档案。

密码登录、忘记密码和密码找回也不在v0.4.0范围；注册页虽然设置密码，但对应入口必须在独立需求、迁移和安全验收完成后才能展示。

## 状态定义

- 拟议：尚未确认；
- 已确认：可以进入技术设计；
- 开发中：已经开始实现；
- 待验收：实现完成但证据未齐；
- 已完成：验收证据齐全；
- 暂缓：保留追溯但不进入当前 MVP；
- 已阻塞：存在明确外部依赖。

## 变更规则

- 产品范围以 v0.4.0 PRD 和 `13-LOGIN-REGISTER-PRD.md` 为准。
- 暂缓需求不能以隐藏页面、必填字段或后端状态门继续阻塞注册后使用。
- 技术内部可以保留迁移期字段，但必须有移除或兼容计划，不能暴露为家庭、身份或监护产品流程。
- 产品范围变化必须同步更新讨论纪要、PRD、功能设计、实施计划和验收文档。
