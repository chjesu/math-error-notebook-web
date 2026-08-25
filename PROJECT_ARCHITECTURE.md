# 李兆霖数学错题本 Web：项目架构

## 1. 当前决策与边界

这是独立 Web 项目。当前只建设并验收本地模拟环境；阿里云部署保留为最后的人工批准步骤，在本地端到端流程完全跑通前不得执行。

当前目标产品基线为 v0.4.0：验证码登录与新用户注册拆成两个页面和两个服务端场景；登录使用手机号验证码，注册使用手机号、验证码、密码和协议确认，注册成功自动登录并进入个人错题本。MVP 仍不建设姓名/昵称/年级资料、身份角色、家庭、学生档案、监护同意或实名认证。个人业务数据以服务端会话解析的 `user_id` 隔离。

v0.4.0 本地测试版的认证契约已落地为四个接口：`POST /v1/auth/login/otp/request`、`POST /v1/auth/login/otp/verify`、`POST /v1/auth/register/otp/request`、`POST /v1/auth/register/complete`。注册原子保存密码凭据、协议版本并创建会话；登录不得建号，注册不得覆盖已有账号。当前 36 paths OpenAPI、真实 MySQL smoke、165 项测试、独立安全复核和浏览器注册/移动视口检查已对齐；旧 v0.3.3 单入口仅作迁移起点。生产门禁仍未完成。详细标准见 `docs/13-LOGIN-REGISTER-PRD.md`。

现有代码和在建 Schema 中的 `tenant_id`、家庭、成员、学生档案及监护同意属于早期设计，不能继续作为产品入口或注册后阻断条件。架构步骤必须形成迁移与回滚方案后再调整，不得直接覆盖用户尚未提交的在建代码。

桌面版的 `data/math_notebook.db` 仍是现有数学题库的权威库，Web 项目不直接读取、复制或共享该 SQLite 文件。后续只能通过只读抽取、哈希对账和受控迁移把已验证内容导入独立 MySQL。

在线身份认证、限流、验证码和权限判断完全由确定性代码与 MySQL 完成。Codex CLI 承担只读工程审查和首次数学图片/判题候选；连续错题会话直接复用官方 Codex app-server Harness 的线程、回合、压缩和事件协议。题干修正、作答 OCR、判题及入本内容均由大模型进行语义确认；确定性代码负责校验资源归属、冻结版本、结构化结果和最终写库。模型不能自行跨过确认口令、直接写入正式错题、发送短信、改变权限或批准部署。

## 2. 整体架构

```mermaid
flowchart TB
    subgraph LOCAL[当前：本地生产等价模拟]
        LB[本地浏览器] -->|localhost| LAPI[ASGI API]
        LAPI --> LAUTH[认证状态机]
        LAUTH --> LDB[(MySQL 8.4\n127.0.0.1:3307)]
        LAUTH --> LCAPTCHA[本地 CAPTCHA 适配器]
        LAUTH --> LSMS[本地模拟短信\n页面标记并自动填入]
    end

    subgraph TARGET[后续：阿里云生产目标]
        U[普通用户] -->|HTTPS| WAF[WAF / SLB]
        WAF --> API[模块化单体 API]
        API --> AUTH[账号与会话]
        API --> DOMAIN[错题本领域服务]
        API --> JOBS[任务编排]
        JOBS --> WORKER[异步 Worker]
        AUTH --> MYSQL[(独立 MySQL 8)]
        DOMAIN --> MYSQL
        WORKER --> MYSQL
        API --> OSS[(OSS 原图/试卷/PDF)]
        AUTH --> CAPTCHA[服务端 CAPTCHA]
        AUTH -->|固定出口 IP| SMS[瑞成云短信]
        API --> OBS[日志/指标/审计]
    end

    DESKTOP[桌面数学错题本\nSQLite 权威题库] -->|只读抽取/哈希对账/受控迁移| DOMAIN
    REVIEW[Codex CLI\nLuna/Terra/Sol] -->|只读结构化候选| GATE[Schema/测试/领域质量门]
    GATE -->|确定性提交| DOMAIN
    GATE -->|确定性提交| JOBS
```

## 3. 功能分解与状态

| 功能域 | 权威入口/计划落点 | 当前状态 |
|---|---|---|
| 手机验证码注册、会话、限流 | `services/web_auth/` | v0.4.0 四接口、密码、协议、场景隔离已在本地验收；短信/CAPTCHA 仍为模拟，生产门禁未完成 |
| 本地 MySQL 模拟环境 | `scripts/local_env.py` | 已实现；仅绑定 localhost，不是生产启动器 |
| Codex 模型路由与团队 | `scripts/codex_task_router.py`、`scripts/codex_app_server.py`、`services/web_app/codex_model.py`、`config/model-routing.json`、`config/team-roles.json` | 已实现；本地启动须使用 `--enable-codex-model` 显式授权，首次 OCR/判题候选走 Terra→Sol 质量门，连续错题会话走官方 app-server 持久 Sol 线程、真实事件流和结构化候选 |
| 任务领取、租约、证据、恢复 | `scripts/project_workflow.py` | 已实现注册模板和全项目模板 |
| 个人账号与 user_id 数据隔离 | `services/web_domain/` | 已实现；API、Store、任务和下载均从服务端会话注入 `user_id` |
| Web 题库、错题、作答和复习数据 | MySQL 个人 `user_id` 模型 | 权威桌面题库已同步到本地 MySQL：10,569 题，其中 10,278 题保持已验证、291 题按授权/质量门降级为候选；生产回滚未完成 |
| 文件上传、解析、审核、判题 | API + Codex CLI 候选 + Codex app-server 会话适配器 | PNG/JPEG 可从一张图片按阅读顺序拆出至多 20 道题及其对应作答，每题建立独立待确认候选→app-server 自然语言追问/修正→确认判题→继续追问/修正→确认入本；模型不可用时安全停止且不显示大表单，PDF/DOCX 自动解析与生产异步 Worker 仍延期 |
| 推荐、复习计划和 PDF | `services/web_domain/` | 仅已验证且授权题可推荐；推荐、复习和 PDF 本地链路已验收，题库为空时展示缺口 |
| 前端/PWA、运营后台和运维恢复 | `web/`、后续运维模块 | 六个侧边栏入口保持独立 URL/HTML；工作台消费 app-server 的真实 NDJSON 事件；线程映射已进 MySQL 并可跨服务重启恢复，消息历史跨刷新/服务端分页、运行中追加/停止/分叉、PWA、运营后台和生产恢复仍延期 |
| 阿里云部署 | WAF/SLB、RDS、OSS、运行环境 | 已后置；本地全链路通过后人工批准 |

## 4. 目录

| 路径 | 权威职责 |
|---|---|
| `services/web_auth/registration.py` | OTP、限流、登录、注册和会话的唯一权威状态机 |
| `services/web_auth/mysql_store.py` | MySQL 事务与行锁 |
| `services/web_auth/asgi.py` | HTTP 边界和 Cookie |
| `services/web_auth/bootstrap.py` | 生产环境变量装配和失败关闭 |
| `services/web_auth/ruicheng_sms.py` | 瑞成云提交适配 |
| `services/web_auth/turnstile.py` | CAPTCHA 服务端验证 |
| `services/web_auth/migrations/` | MySQL 迁移 |
| `services/web_domain/` | 错题、推荐、复习调度、进度和 A4 PDF |
| `scripts/project_workflow.py` | 注册/全项目任务模板、依赖、领取、租约和证据 |
| `scripts/codex_task_router.py` | Luna/Terra/Sol 分层只读审查 |
| `scripts/local_env.py` | localhost MySQL、模拟短信/CAPTCHA 与端到端验收 |
| `config/model-routing.json` | 模型路由策略 |
| `schemas/engineering-review-result.schema.json` | 结构化审查输出 |
| `docs/` | 产品、架构、实施、验收和运维基线 |

## 5. 注册链路

```mermaid
sequenceDiagram
    participant C as 客户端
    participant A as Auth API
    participant D as MySQL
    participant T as Turnstile
    participant S as 瑞成云
    C->>A: 按 login/register 场景请求验证码
    A->>D: 原子检查冷却、多维限额和预算
    opt 风险达到升级阈值
        A->>T: 服务端验证一次性 token
        T-->>A: success + hostname + action
    end
    A->>D: 创建挑战并预留发送次数
    A->>S: 单次 POST，3s/5s 超时，不重试
    S-->>A: 00/03 + 流水号
    A->>D: 标记 sent
    A-->>C: 统一 202 响应
    C->>A: 登录提交验证码；注册提交验证码、密码和协议版本
    A->>D: 行锁校验、场景隔离、单次消费、建用户/凭据/会话
    A-->>C: Secure/HttpOnly Cookie
```

## 6. Codex CLI 与质量门

```mermaid
flowchart LR
    TASK[冻结且脱敏的任务包] --> ROUTER{任务难度}
    ROUTER -->|需求/文案| LUNA[Luna low]
    ROUTER -->|一般实现| TERRA[Terra medium]
    ROUTER -->|认证/迁移/争议| SOL[Sol high]
    LUNA --> CANDIDATE[结构化候选]
    TERRA --> CANDIDATE
    SOL --> CANDIDATE
    CANDIDATE --> CHECK[Schema + 测试 + 领域复核]
    CHECK -->|通过| COMMIT[确定性提交]
    CHECK -->|不通过| REWORK[返工/人工复核]
```

模型任务统一走 `scripts/codex_task_router.py`，外发前必须显式授权。团队岗位和波次由 `config/team-roles.json` 声明；每个岗位对应一个独立、临时、只读的 Codex CLI 子进程，最多并行 4 个，完整职责见 `docs/14-CODEX-MULTI-AGENT-TEAM.md`。批量 `team-run` 只接受位于固定输入根目录、按岗位拆分的公开/合成资料包，并把结果限制到固定候选根目录；真实项目材料不得用该命令批量外发。手机号、验证码、密码、密钥和数据库凭据永不进入模型输入。本地数学流程只发送当前用户已授权处理的题目图片或已确认题干/作答，且移除 `user_id` 等身份字段；图片外发前必须复验隔离对象哈希、解码并重编码为去 EXIF 的有界 PNG 预览。同一任务资源与输入版本只允许一个进行中的调用，全局最多两个；图片识别结果必须按 `item_no` 连续编号，服务端在一个事务中把同一文件的每道题保存为独立 `intake_item`，输出匹配 JSON Schema、资源 ID 和冻结版本后才能进入待确认状态。单项低置信度最多自动升级一次，批量波次不自动升级。

本地 Web 启动的 CLI 复用桌面用户的 Codex 登录目录，并只继承标准代理、上游代理和 CA 证书环境，不复制短信、数据库等业务秘密；调用仍带 `--ignore-user-config`，避免加载用户配置中的任意 MCP、工具或提示。瞬时连接、超时和限流错误总计最多尝试两次；证书、认证和非网络 CLI 错误不做盲目重试。每次尝试写入 `data/audits/codex-routing/codex-cli-events.jsonl`，仅记录调用标识、任务、模型、阶段、耗时、尝试次数、结果和错误分类，不记录题目、图片路径、提示词、stdout、stderr、账号或密钥。

错题会话使用 `math-notebook-loop` 路由和官方 `codex app-server`：首轮通过 `thread/start` 建立持久线程，后续轮次只按 MySQL 中当前用户与 intake 绑定的不透明 thread id 调用 `thread/resume`；浏览器不能读取或提交 thread id。运行时固定只读 sandbox、拒绝审批、关闭 shell 与 MCP，最终消息必须通过 `math-loop-turn.schema.json`。真实 turn/item/delta/压缩/完成事件经 NDJSON 推送；详细能力边界见 `docs/16-CODEX-APP-SERVER-HARNESS.md`。

## 7. 完成门

“本地完整测试版”当前指：登录/注册→个人空工作台→上传 PNG/JPEG→Codex 识别候选→在同一会话中修正并确认题干与作答→Codex 判题候选→继续追问/修正→用户确认入本→仅已验证推荐→复习→PDF；模型失败时安全停止，不自动写入且不回退到页面大表单。导出/注销的敏感 OTP 二次验证、会话撤销、下载和任务/文件失效也必须通过本地 smoke 与独立安全复核。该闭环和权威题库本地同步不等于生产完成；真实短信/CAPTCHA/KMS/OSS、生产异步 Worker、PDF/DOCX 自动解析、跨刷新消息恢复、压测/灾备/观测和生产恢复仍是门禁。

阿里云部署只有在上述本地流程完全跑通、系统安全复核通过且用户人工批准后才能领取 `cloud_deploy_approval`。

## 本地模拟边界

本地模拟复用同一注册状态机、MySQL 适配器、迁移和 ASGI 边界，仅替换短信与 CAPTCHA 外部适配器。localhost 启动器会在验证码申请成功的响应中附加本地测试码，页面明确标记并自动填入；生产装配不会返回该字段。使用 `python -X utf8 -B scripts/local_env.py serve --enable-codex-model` 时，才启用外部 Codex CLI 数学候选与连续会话；不带该参数时模型接口稳定返回 `model_unavailable`，前端安全停止。同步 MySQL、文件、PDF 和 Codex CLI 调用通过标准库线程执行，避免阻塞 ASGI 事件循环；上传仍使用有大小上限的本地缓冲，生产必须改为流式 OSS。MySQL 固定绑定 `127.0.0.1:3307`；模拟服务固定绑定 localhost，不能作为生产启动入口。
