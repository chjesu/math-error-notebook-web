# 李兆霖数学错题本 Web

个人数学错题本的独立 Web 项目。v0.4.0 本地候选冻结四个认证接口：登录验证码申请/验证、注册验证码申请、注册完成；注册保存密码和协议版本并自动登录。用户不填写昵称、年级或其他资料，不选择身份、不创建家庭，也不经过监护或实名流程。本地完整学习闭环及只读脱敏运营后台已通过 230 项测试；真实 MySQL smoke、官方 Codex app-server 连续会话/图片识别/判题实测和浏览器学习流程均已验收，生产门禁仍未完成。

本仓库不包含正式数学题库语料、错题照片或短信密钥。题库只能通过受控迁移接入；本地没有合格推荐题时会明确显示缺口。

## 快速检查

```powershell
python -X utf8 -B scripts/project_workflow.py doctor --json
python -X utf8 -B scripts/project_workflow.py status WEB-PRD-003 --json
python -X utf8 -B -m unittest discover -s tests -p "test_*.py"
python -X utf8 -B scripts/codex_task_router.py route --task web-security-review --json
```

## 本地模拟环境

本地环境使用真实 MySQL 8，但短信和 CAPTCHA 均为模拟实现，不连接供应商、不产生费用：

新电脑可直接执行以下命令完成依赖安装、空白数据初始化、smoke 和启动：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\bootstrap_local.ps1
```

完整前置资源、空白数据范围和可选题库导入见 [新电脑本地初始化](docs/19-NEW-COMPUTER-BOOTSTRAP.md)。

```powershell
.\.venv\Scripts\python.exe -X utf8 -B scripts\local_env.py init
.\.venv\Scripts\python.exe -X utf8 -B scripts\local_env.py smoke
.\.venv\Scripts\python.exe -X utf8 -B scripts\local_env.py serve --enable-harness-model --enable-harness-ui
```

服务仅监听 `127.0.0.1:8000`。请求验证码后，页面会明确标记“仅限本地测试”并自动填入模拟验证码；正式服务响应不会返回验证码。测试 CAPTCHA token 为 `local-captcha`。停止数据库使用：

默认不向外部模型发送材料。当前用户明确授权本地测试时，可使用 `python -X utf8 -B scripts/local_env.py serve --enable-harness-model --enable-harness-ui` 同时启用固定版 DeepSeek Harness 核心和官方 Web 前端。工作台直接装载 `@deepseek-ai/dsh-web-frontend@0.1.1-rc.2`，不是仿写页面；会话、历史分页、附件、上下文计量、自动压缩、工作区、设置和输入交互均由官方 Host 提供。供应商、网关、协议、模型、密钥环境变量名和文本/图片能力均可配置；切换到 Qwen 等 OpenAI 兼容通道不复制错题流程。旧的 `--enable-codex-model` 仅保留作对照。PNG/JPEG 先生成识别候选，上传后系统按顺序自动冻结版本并判题；正确题自动跳过，错误或部分正确在确定性版本门和写库门校验后自动入本，无法识别或证据冲突时停留等待补充。外发前复验上传哈希，并仅发送去元数据、限尺寸的重编码预览图。

```powershell
.\.venv\Scripts\python.exe -X utf8 -B scripts\local_env.py stop
```

随机本地密码、数据文件和日志只保存在 Git 忽略的 `.runtime/local-mysql/`，禁止复制到生产环境。

需要模型审查时，先用 `scripts/prepare_review_packet.py --file <显式文件>` 生成冻结、限长、密钥检查过的 JSON，再在确认外发内容后运行路由器的 `run --authorize-external-send`。

## 后续生产部署（当前不执行）

当前 localhost + MySQL 8 的个人错题本支持：上传→可配置 Harness 批量识别候选→系统自动逐题冻结版本→独立解题与判题复核→正确题跳过、错误或部分正确自动入本→仅已验证推荐→复习→PDF；同一 Harness 会话支持自然语言修正和追问。每个账号按中国标准时间显示今日学习负荷：判题建议 12 道、上限 20 道，推荐题建议 12 道、上限 24 道，可覆盖一次选取的 12 道错题并为每题最多匹配 2 道练习；失败、无法识别、重试和重复提交不重复计数，同一批图片不会处理到一半被截断。工作台接收真实运行时事件，线程映射可跨 Web 服务重启恢复，刷新后会恢复最近一次会话的产品消息，无法识别、证据冲突或模型不可用时安全停止。导出为业务 JSON+文件元数据（不含上传原始二进制，最多下载 3 次并审计），注销执行停用、业务失效和对象文件删除，认证/协议/审计按策略留存。独立运营入口已提供按角色过滤的脱敏概览、失败任务、内容质量、短信风控、隐私工单和访问审计。运行中追加/分叉控制、生产异步 Worker、PDF/DOCX 自动解析、真实短信/CAPTCHA/KMS/OSS、PWA、人工复核提交、敏感访问审批、危险批量操作、压测/灾备/观测、正式部署和生产恢复均延期，不得写成完成。

1. 在 MySQL 8 执行 `services/web_auth/migrations/0001_phone_registration.sql`。
2. 从密钥管理服务注入 `services/web_auth/README.md` 列出的环境变量。
3. 安装锁定并审查后的生产依赖。
4. 冻结完整 `NotebookAsgiApp` 的生产装配、可信代理和 Worker 启动入口；当前 `local_env.py serve` 与认证子应用启动器都不得直接用于生产。

上线前必须完成 `docs/05-TEST-ACCEPTANCE-OPERATIONS.md` 的真实 MySQL 并发、短信小流量、枚举时序、成本熔断和回滚验收。

## 文档

- [项目架构](PROJECT_ARCHITECTURE.md)
- [产品需求](docs/02-PRODUCT-REQUIREMENTS.md)
- [产品功能设计](docs/09-PRODUCT-FUNCTION-DESIGN.md)
- [技术架构](docs/03-TECHNICAL-ARCHITECTURE.md)
- [实施计划](docs/04-IMPLEMENTATION-PLAN.md)
- [测试与运维](docs/05-TEST-ACCEPTANCE-OPERATIONS.md)
- [角色、任务领取与模型工作流](docs/06-WORKBREAKDOWN-CODEX-WORKFLOW.md)
- [瑞成云接入](docs/07-SMS-PROVIDER-RUICHENG.md)
- [当前实施证据与剩余上线门](docs/08-IMPLEMENTATION-EVIDENCE.md)
- [可配置 Harness 运行时](docs/20-CONFIGURABLE-HARNESS-RUNTIME.md)
- [阿里云部署与上线手册](docs/22-ALIYUN-DEPLOYMENT-RUNBOOK.md)
- [Web 定价策略讨论记录](docs/23-PRICING-STRATEGY.md)
- [最小运营后台产品基线](docs/24-OPERATIONS-ADMIN-PRD.md)
