# 李兆霖数学错题本 Web

个人数学错题本的独立 Web 项目。v0.4.0 本地候选冻结四个认证接口：登录验证码申请/验证、注册验证码申请、注册完成；注册保存密码和协议版本并自动登录。用户不填写昵称、年级或其他资料，不选择身份、不创建家庭，也不经过监护或实名流程。本地完整学习闭环已验收：真实 MySQL smoke、166 项测试、官方 Codex app-server 连续会话/图片识别/判题实测和浏览器全流程均通过；生产门禁仍未完成。

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

```powershell
.\.venv\Scripts\python.exe -X utf8 -B scripts\local_env.py init
.\.venv\Scripts\python.exe -X utf8 -B scripts\local_env.py smoke
.\.venv\Scripts\python.exe -X utf8 -B scripts\local_env.py serve
```

服务仅监听 `127.0.0.1:8000`。请求验证码后，页面会明确标记“仅限本地测试”并自动填入模拟验证码；正式服务响应不会返回验证码。测试 CAPTCHA token 为 `local-captcha`。停止数据库使用：

默认不向外部模型发送材料。当前用户明确授权本地测试时，可使用 `python -X utf8 -B scripts/local_env.py serve --enable-codex-model` 启用数学候选与官方 Codex app-server 持续会话：PNG/JPEG 先生成识别候选，上传后底部输入框进入持久线程，可用自然语言修正题干、作答 OCR、判题和解法；“确认并判题”“确认入本”分别经过确定性版本门和写库门。外发前复验上传哈希，并仅发送去元数据、限尺寸的重编码预览图；同资源/版本只允许一个进行中的调用，全局同时最多两个；任何候选都不会自动进入正式错题。

```powershell
.\.venv\Scripts\python.exe -X utf8 -B scripts\local_env.py stop
```

随机本地密码、数据文件和日志只保存在 Git 忽略的 `.runtime/local-mysql/`，禁止复制到生产环境。

需要模型审查时，先用 `scripts/prepare_review_packet.py --file <显式文件>` 生成冻结、限长、密钥检查过的 JSON，再在确认外发内容后运行路由器的 `run --authorize-external-send`。

## 后续生产部署（当前不执行）

当前 localhost + MySQL 8 的个人错题本支持：上传→Codex CLI 识别候选→官方 app-server 同一线程自然语言修正→确认判题→Codex CLI 判题候选→继续追问/修正→确认入本→仅已验证推荐→复习→PDF；工作台接收真实 Harness 事件，线程映射可跨 Web 服务重启恢复，模型不可用时安全停止。导出为业务 JSON+文件元数据（不含上传原始二进制，最多下载 3 次并审计），注销执行停用、业务失效和对象文件删除，认证/协议/审计按策略留存。跨刷新消息历史恢复、服务端历史分页、运行中追加/停止/分叉控制、生产异步 Worker、PDF/DOCX 自动解析、真实短信/CAPTCHA/KMS/OSS、PWA、运营后台、压测/灾备/观测、正式部署和生产恢复均延期，不得写成完成。

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
