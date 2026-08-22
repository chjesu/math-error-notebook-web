# 李兆霖数学错题本 Web 版文档索引

> 文档基线：v0.3.2
> 基线日期：2026-08-22
> 当前状态：单一用户产品基线已确认；注册服务可运行，直接进入错题本和完整学习闭环待实现

## 当前产品口径

1. 所有用户使用手机号验证码注册或登录。
2. 验证成功后直接进入个人错题本。
3. 不填写姓名、昵称、年级、年龄、身份或其他个人资料。
4. 不做家庭、成员、学生档案、监护、实名或普通用户角色。
5. 一个账号对应一个私有错题本，所有数据以服务端 user_id 隔离。
6. 上传、判题、错因和推荐先作为候选，确认和质量门后才成为正式记录。
7. 推荐只使用已验证且授权允许的题。
8. 云部署继续后置，必须先完成本地全链路与安全验收。

## 文档导航

| 文档 | 用途 | 版本 | 状态 |
|---|---|---:|---|
| [01-DISCUSSION-MINUTES.md](./01-DISCUSSION-MINUTES.md) | 当前决策、范围和被替代决策 | v0.3.2 | 已确认 |
| [02-PRODUCT-REQUIREMENTS.md](./02-PRODUCT-REQUIREMENTS.md) | 活跃需求、验收标准、指标和暂缓需求 | v0.3.2 | 已确认 |
| [03-TECHNICAL-ARCHITECTURE.md](./03-TECHNICAL-ARCHITECTURE.md) | 系统架构、注册状态机、MySQL 与安全边界 | v0.2.0 | 待按 v0.3.2 修订 |
| [04-IMPLEMENTATION-PLAN.md](./04-IMPLEMENTATION-PLAN.md) | 分批实施、迁移和完成证据 | v0.3.2 | 待验收 |
| [05-TEST-ACCEPTANCE-OPERATIONS.md](./05-TEST-ACCEPTANCE-OPERATIONS.md) | 安全与学习闭环验收 | v0.3.2 | 已确认 |
| [06-WORKBREAKDOWN-CODEX-WORKFLOW.md](./06-WORKBREAKDOWN-CODEX-WORKFLOW.md) | 角色、任务领取与模型路由 | v0.2.0 | 待按 v0.3.2 修订 |
| [07-SMS-PROVIDER-RUICHENG.md](./07-SMS-PROVIDER-RUICHENG.md) | 短信适配、密钥和网络约束 | v0.2.0 | 待验收 |
| [08-IMPLEMENTATION-EVIDENCE.md](./08-IMPLEMENTATION-EVIDENCE.md) | 当前实现证据与剩余门禁 | v0.2.0 | 待按 v0.3.2 更新 |
| [09-PRODUCT-FUNCTION-DESIGN.md](./09-PRODUCT-FUNCTION-DESIGN.md) | 页面、流程、异常恢复和产品事件 | v0.3.2 | 已确认 |

## 活跃需求

| 编号 | 主题 | MVP |
|---|---|---|
| ACCOUNT-001 | 注册后直接使用 | 是 |
| AUTH-001、AUTH-002 | 手机号验证和防轰炸 | 是 |
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

## 状态定义

- 拟议：尚未确认；
- 已确认：可以进入技术设计；
- 开发中：已经开始实现；
- 待验收：实现完成但证据未齐；
- 已完成：验收证据齐全；
- 暂缓：保留追溯但不进入当前 MVP；
- 已阻塞：存在明确外部依赖。

## 变更规则

- 产品范围以 v0.3.2 PRD 为准。
- 暂缓需求不能以隐藏页面、必填字段或后端状态门继续阻塞注册后使用。
- 技术内部可以保留迁移期字段，但必须有移除或兼容计划，不能暴露为家庭、身份或监护产品流程。
- 产品范围变化必须同步更新讨论纪要、PRD、功能设计、实施计划和验收文档。
