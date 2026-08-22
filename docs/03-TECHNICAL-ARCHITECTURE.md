# Web 版技术架构（v0.3.2）

## 1. 冻结边界

李兆霖数学错题本 Web 首版只有一种用户：完成手机号验证码后，直接使用自己的错题本。服务端从安全 Cookie 解析当前 `user_id`；客户端不得提交可改变数据归属的 `user_id`。

首版不建设姓名、昵称、年级、身份角色、家庭、学生档案、监护同意或实名认证。旧设计中的 tenant/student/guardian 只作为迁移历史，不是产品、权限或注册状态。

## 2. 最小架构

```mermaid
flowchart LR
    B[浏览器] -->|HTTPS + HttpOnly Cookie| API[模块化单体 ASGI]
    API --> AUTH[验证码与会话]
    API --> DOMAIN[个人错题本]
    API --> FILES[文件隔离区]
    API --> JOBS[可恢复任务]
    AUTH --> DB[(MySQL 8)]
    DOMAIN --> DB
    JOBS --> DB
    FILES --> STORE[(本地受控目录 / OSS)]
    JOBS --> REVIEW[离线只读模型审查]
```

首版不引入 Redis、消息中间件或微服务。任务用 MySQL 行锁、状态和检查点恢复。模型只产生带输入版本的候选结果，不能登录、授权、发短信或直接写正式记录。

## 3. 权威边界

| 边界 | 权威实现 | 不变量 |
|---|---|---|
| OTP/注册 | `services/web_auth/registration.py` | 验证成功即创建或登录账号；挑战只能成功消费一次 |
| 会话 | Auth Store/API | 仅保存 token 哈希；Cookie 为 Secure、HttpOnly、SameSite=Lax |
| 个人数据 | Domain Store | 所有查询和写入都由会话注入 `user_id` |
| 公共题库 | `question_*` 表 | 只有来源允许且已验证版本可进入推荐池 |
| 文件 | File service/Store | 对象键不可猜；同用户幂等，跨用户不复用对象 |
| 候选提交 | Domain service | 候选携带 `input_version`；正式提交时必须再次比对 |
| 迁移 | `scripts/local_env.py` | 文件哈希、成功后记账、失败不记账、重复执行幂等 |

## 4. 账号与授权

`POST /v1/auth/otp/request` 使用统一 202 响应避免枚举。`POST /v1/auth/otp/verify` 只接收手机号、挑战 token 和验证码；成功后设置会话 Cookie 并返回 `next_action=workbench`。新手机号创建最小账号，旧手机号登录同一账号。

每个受保护请求先解析会话，再把 `user_id` 传给 Store。资源查询必须同时匹配 `id` 与 `user_id`；未匹配统一返回 `not_found`，不暴露其他用户资源是否存在。退出当前设备撤销当前会话，全端退出撤销该用户所有会话。

保留 `tenant_scope_hash` 仅作为旧 0001 中公开注册入口的内部反滥用维度；它不能成为产品租户或数据权限键。

## 5. 数据与状态

公共内容：`question_sources → questions → question_versions → question_verifications`。个人内容：`web_files`、`intake_items`、`web_jobs`、`attempts`、`grade_candidates`、`error_notebook_entries`、`recommendations`、`review_tasks`，全部直接外键到 `web_users.id`。

首题闭环：

```mermaid
stateDiagram-v2
    [*] --> uploaded
    uploaded --> extracting
    extracting --> waiting_confirmation
    waiting_confirmation --> grading: 用户确认题干与作答
    grading --> grade_ready
    grade_ready --> committed: 用户确认入本且 input_version 未变化
    extracting --> failed_retryable
    grading --> failed_retryable
    failed_retryable --> extracting: 重试提取
    failed_retryable --> grading: 重试判题
    uploaded --> cancelled
    waiting_confirmation --> cancelled
```

`unclear` 判题不能入本。相同幂等键只产生一个上传、作答、正式错题或复习阶段。候选结果永不覆盖输入；用户修订会增加 `input_version`，使旧候选失效。

## 6. 迁移顺序

1. 已应用的 `0001_phone_registration.sql` 永不改写。
2. 尚未共享应用的 `0002_web_domain.sql` 直接重构为 `user_id` 模型。
3. `0003_account_simplification.sql` 前向移除资料和监护字段/表，并收敛账号状态。
4. 迁移账本以名称和 SHA-256 记录；同名异哈希立即失败。

空库执行 0001→0002→0003。已有本地库先登记已存在且哈希一致的 0001，再执行剩余迁移。DDL 失败不得记成功账；修复后从未记账迁移恢复。回滚通过恢复执行前备份或补充新的前向迁移完成，不逆向修改已经发布的迁移。

## 7. API 与错误

机器可校验契约见 `openapi/web-v1.json`，详细权限、Schema、状态与迁移约束见 `docs/12-ARCHITECTURE-DATA-API-CONTRACT.md`。错误统一为 `{error:{code,message,retryable,request_id}}`；页面只依赖稳定 `code`，不解析文案。

## 8. 安全和部署门

手机号、验证码、Cookie、CAPTCHA token 和密钥不得进入日志或仓库。供应商只支持明文 HTTP 时，生产必须固定出口 IP、供应商白名单、出站安全组、短超时且禁止自动重发和请求体日志。当前只验收本地生产等价环境；阿里云部署仍需单独人工批准。
