# v0.4.0 目标架构、数据与 API 契约

> 当前实现说明：业务领域23条路径和认证单入口仍由 `openapi/web-v1.json` v0.3.3描述。以下认证部分是v0.4.0目标，完成代码、迁移和测试前不得把OpenAPI或认证模块标记为v0.4.0完成。

## 1. 权限矩阵（ARCH-001）

| 操作 | 未登录 | 当前用户 | 其他用户资源 |
|---|---:|---:|---:|
| 请求/验证登录 OTP | 允许，仅已有账号场景 | 允许 | 不适用 |
| 请求注册 OTP / 完成注册 | 允许，仅新账号场景 | 已登录时拒绝或要求先退出 | 不适用 |
| 查看工作台、文件、任务、错题 | 401 | 允许 | 404 |
| 修改候选、确认入本、重试任务 | 401 | 允许 | 404 |
| 获取推荐、完成复习、查看进度 | 401 | 允许 | 404 |
| 创建或下载练习 PDF | 401 | 允许 | 404 |
| 退出当前设备 | 401 | 允许 | 不适用 |
| 全端退出 | 401 | 允许 | 不适用 |
| 读取已验证公共题库 | 按接口策略 | 允许 | 不适用 |

规则：业务请求不得包含归属型 `user_id`；对象 ID 只是定位键，不是授权。账号状态只有 `active`、`locked`、`pending_delete`、`deleted`，只有 `active` 可进入工作台。

## 2. 数据契约（ARCH-002）

```mermaid
erDiagram
    WEB_USERS ||--o{ AUTH_SESSIONS : opens
    WEB_USERS ||--o{ WEB_FILES : owns
    WEB_USERS ||--o{ INTAKE_ITEMS : owns
    WEB_USERS ||--o{ WEB_JOBS : runs
    WEB_USERS ||--o{ ATTEMPTS : makes
    ATTEMPTS ||--o{ GRADE_CANDIDATES : produces
    WEB_USERS ||--o{ ERROR_NOTEBOOK_ENTRIES : owns
    ERROR_NOTEBOOK_ENTRIES ||--o{ RECOMMENDATIONS : receives
    ERROR_NOTEBOOK_ENTRIES ||--o{ REVIEW_TASKS : schedules
    REVIEW_TASKS ||--o{ REVIEW_ATTEMPTS : records
    QUESTION_SOURCES ||--o{ QUESTIONS : contains
    QUESTIONS ||--o{ QUESTION_VERSIONS : versions
    QUESTION_VERSIONS ||--o{ QUESTION_VERIFICATIONS : verifies
```

约束：

- 个人表必须有 `user_id NOT NULL` 和面向 `user_id` 的查询索引；外键直接引用 `web_users(id)`。
- Store 的个人资源方法第一个命名参数必须是服务端会话给出的 `user_id`。
- `web_files(user_id,purpose,content_sha256)`、`attempts(user_id,idempotency_key)`、`error_notebook_entries(user_id,attempt_id)`、`review_tasks(user_id,error_id,stage)`、`review_attempts(user_id,idempotency_key)` 唯一。
- 公共题库不带个人 `user_id`；只有 `questions.status=verified`、来源授权允许、且存在 verified verification 的版本可推荐。
- 删除旧 `web_tenants`、`tenant_memberships`、`web_students`、`tenant_invitations` 产品表；不做兼容视图。

## 3. 候选/正式与版本（ARCH-003）

| 记录 | 可修改 | 成为正式记录的门 |
|---|---|---|
| 上传文件 | 否；删除为状态变化 | 安全检查为 ready |
| `intake_items` 识别候选 | 自动解析或用户手工补录后可修订；每次 `input_version+1` | 用户确认当前版本 |
| `grade_candidates` 判题候选 | 模型不可覆盖输入；本地手工候选明确记录为用户输入 | `input_version` 相等且 verdict 非 unclear |
| `error_notebook_entries` 正式错题 | 只追加审计更新 | 用户显式确认；按 attempt 唯一 |
| 题库候选版本 | 只追加版本 | 来源允许 + 独立/人工验证 |

正式提交事务必须锁定 intake、attempt 和候选，重新比较 `input_version`，再写正式错题和审计事件。重复提交返回同一正式记录。任务状态只允许 `queued → running → waiting_confirmation/completed/failed_retryable/failed_final/cancelled`；失败保留最后检查点。

## 4. API 与错误（ARCH-004）

当前机器契约：`openapi/web-v1.json`。当前23条路径覆盖单入口OTP、会话、工作台、文件与任务、自动不可用时的手工识别/判题候选、正式入本、错题、已验证推荐、今日复习、进度以及练习PDF创建/授权下载。

v0.4.0认证目标端点为：

| 方法与路径 | 目标行为 |
|---|---|
| `POST /v1/auth/login/otp/request` | 仅已注册账号申请 `purpose=login` 挑战 |
| `POST /v1/auth/login/otp/verify` | 验证登录挑战、协议确认并创建会话，不创建账号 |
| `POST /v1/auth/register/otp/request` | 仅未注册手机号申请 `purpose=register` 挑战 |
| `POST /v1/auth/register/complete` | 验证注册挑战并原子创建账号、密码凭据、协议记录和会话 |

旧认证端点的停用、兼容期和回滚必须在升级OpenAPI时明确；不得让新旧端点同时绕过同一手机号和平台预算。

| 错误码 | HTTP | 页面行为 |
|---|---:|---|
| `invalid_request` | 400 | 定位字段，保留用户输入 |
| `invalid_code` | 400 | 清空验证码，保留当前页其他可安全复用字段 |
| `code_expired` | 400 | 清空验证码和挑战，要求重新获取 |
| `too_many_attempts` | 400 | 当前挑战作废，要求重新获取 |
| `phone_not_registered` | 409 | 登录页提示并提供注册跳转，回填手机号 |
| `phone_already_registered` | 409 | 注册页提示并提供登录跳转，不提交密码 |
| `agreement_required` | 400 | 聚焦协议复选框 |
| `weak_password` | 400 | 聚焦密码字段，说明6—20位规则 |
| `request_too_large` | 413 | 保留本地文件选择并提示大小限制 |
| `authentication_required` | 401 | 回到手机号入口；成功后返回原页 |
| `forbidden` | 403 | 不重试，提示无操作权限 |
| `not_found` | 404 | 显示不存在；跨用户同样返回此码 |
| `conflict` | 409 | 刷新资源后重试 |
| `input_version_changed` | 409 | 丢弃旧候选并重新分析 |
| `waiting_confirmation` | 409 | 打开确认页 |
| `rate_limited` | 429 | 按 `Retry-After` 等待 |
| `captcha_required` | 428 | 原地显示 CAPTCHA |
| `temporarily_unavailable` | 503 | 显示可恢复状态，不自动重发短信 |
| `failed_retryable` | 503 | 从检查点重试 |
| `failed_final` | 422 | 保留数据并给出修正入口 |

## 5. 迁移与回滚（ARCH-005）

- 0001 已发布：只读；旧字段由 0003 前向移除。
- 0002、0003已在本地MySQL应用；0004纯增量增加幂等复习结果历史，不修改既有记录。
- v0.4.0必须新增下一编号的前向认证迁移，建立密码凭据、协议确认和挑战场景约束；禁止改写0001—0004。
- 旧账号没有密码时仍可验证码登录；不得生成默认密码，也不得因迁移阻断登录。
- 迁移表记录 `name`、`sha256`、`applied_at`；DDL 完成后才写账本。
- 空库、已有 0001、本地重复运行、同名异哈希、DDL 中途失败五种演练必须有自动化证据。
- 生产回滚不是执行破坏性 down SQL，而是恢复部署前 MySQL/OSS 一致性备份；数据修复用新编号前向迁移。

## 6. 文档与工作流（ARCH-006）

`WEB-PRD-004`、v0.4.0 PRD和`docs/13-LOGIN-REGISTER-PRD.md`是当前目标产品基线。现有`WEB-PRD-003`和v0.3.3 OpenAPI仅保留为已实现迁移起点；认证重构必须继续更新`WEB-PRD-004`证据，使本契约、WBS、OpenAPI和迁移任务ID一致。
