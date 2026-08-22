# 李兆霖数学错题本 Web

多用户 Web 版的独立后端项目。当前首个可运行模块是手机验证码注册/登录，包含多维防轰炸、MySQL 持久状态、瑞成云短信、Turnstile 服务端校验、安全 Cookie、未成年人监护同意门和分层 Codex CLI 审查工作流。

本仓库不包含数学 SQLite 题库、错题照片、PDF 或短信密钥。题库能力后续通过受控领域服务接入。

## 快速检查

```powershell
python -X utf8 -B scripts/project_workflow.py doctor --json
python -X utf8 -B scripts/project_workflow.py status WEB-PROJECT-001 --json
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

服务仅监听 `127.0.0.1:8000`。请求验证码后，模拟验证码显示在当前终端。测试 CAPTCHA token 为 `local-captcha`。停止数据库使用：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B scripts\local_env.py stop
```

随机本地密码、数据文件和日志只保存在 Git 忽略的 `.runtime/local-mysql/`，禁止复制到生产环境。

需要模型审查时，先用 `scripts/prepare_review_packet.py --file <显式文件>` 生成冻结、限长、密钥检查过的 JSON，再在确认外发内容后运行路由器的 `run --authorize-external-send`。

## 后续生产部署（当前不执行）

当前先在 localhost + MySQL 8 的模拟环境跑通全部功能。只有本地端到端验收和系统安全复核完成，并获得人工批准后，才执行以下阿里云生产步骤。

1. 在 MySQL 8 执行 `services/web_auth/migrations/0001_phone_registration.sql`。
2. 从密钥管理服务注入 `services/web_auth/README.md` 列出的环境变量。
3. 安装 `requirements.txt`。
4. 由可信 SLB/WAF 终止 HTTPS，并启动：

```powershell
uvicorn services.web_auth.bootstrap:create_app --factory --host 127.0.0.1 --port 8000 `
  --proxy-headers --forwarded-allow-ips=<SLB内网IP>
```

上线前必须完成 `docs/05-TEST-ACCEPTANCE-OPERATIONS.md` 的真实 MySQL 并发、短信小流量、枚举时序、成本熔断和回滚验收。

## 文档

- [项目架构](PROJECT_ARCHITECTURE.md)
- [产品需求](docs/02-PRODUCT-REQUIREMENTS.md)
- [技术架构](docs/03-TECHNICAL-ARCHITECTURE.md)
- [实施计划](docs/04-IMPLEMENTATION-PLAN.md)
- [测试与运维](docs/05-TEST-ACCEPTANCE-OPERATIONS.md)
- [角色、任务领取与模型工作流](docs/06-WORKBREAKDOWN-CODEX-WORKFLOW.md)
- [瑞成云接入](docs/07-SMS-PROVIDER-RUICHENG.md)
- [当前实施证据与剩余上线门](docs/08-IMPLEMENTATION-EVIDENCE.md)
