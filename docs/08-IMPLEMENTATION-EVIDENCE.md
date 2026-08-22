# 注册系统实施证据

日期：2026-08-22

## 已完成

| 要求 | 证据 |
|---|---|
| 手机验证码注册/登录 | `services/web_auth/registration.py`、`asgi.py` |
| 防验证码轰炸 | 手机 60 秒冷却、5 次/小时、5 次/滚动 24 小时；IP 10 次/分钟、20 次/小时；设备 10 次/小时、20 次/日，并叠加 IP 前缀、租户和全局限额 |
| MySQL 持久状态 | `mysql_store.py`、`migrations/0001_phone_registration.sql` |
| 瑞成云通道 | `ruicheng_sms.py`；单次 POST、3/5 秒超时、不自动重试 |
| CAPTCHA | `turnstile.py`；服务端校验 success、hostname、action，失败关闭 |
| 安全 HTTP 边界 | HTTPS、Host、JSON、请求体限制、可信代理 IP、安全 Cookie |
| 系统架构图 | `PROJECT_ARCHITECTURE.md`、`docs/03-TECHNICAL-ARCHITECTURE.md` |
| 功能分解和角色 | `docs/04-IMPLEMENTATION-PLAN.md`、`docs/06-WORKBREAKDOWN-CODEX-WORKFLOW.md` |
| 任务领取与断点恢复 | `scripts/project_workflow.py` |
| Codex CLI 分层路由 | `scripts/codex_task_router.py`、`config/model-routing.json` |

## 自动化验证

- `.venv\\Scripts\\python.exe -X utf8 -B -m unittest discover -s tests -p "test_*.py"`：53 项通过。
- `python -X utf8 -B scripts/project_workflow.py doctor --json`：`status=ok`，无缺失文件。
- 使用虚构环境变量调用 `services.web_auth.bootstrap:create_app`：成功创建 `AuthAsgiApp`，未连接真实 MySQL、Turnstile 或短信网关。
- Codex 路由预览：需求为 Luna/low，普通实现为 Terra/medium；认证风险提升为 Sol/high。

## 本地集成验证

- Oracle MySQL Server 8.4.9 仅监听 `127.0.0.1:3307`，正式迁移已执行；应用使用专用低权限账号。
- `scripts/local_env.py smoke` 使用真实 MySQL、模拟短信与模拟 CAPTCHA 完成验证码请求、注册、Secure Cookie、验证码单次消费验证。
- 50 个同手机号并发请求仅产生 1 次模拟短信；同 IP 一分钟 11 个请求仅产生 10 次模拟短信。
- 本地 Uvicorn 真实监听 `127.0.0.1:8000`，`GET /healthz` 返回 `200`、`Cache-Control: no-store`。
- 经 localhost HTTP 实际请求，`POST /v1/auth/otp/request` 返回 `202`，读取终端模拟码后 `POST /v1/auth/otp/verify` 返回 `200` 和 `HttpOnly; Secure; SameSite=Lax` 会话 Cookie。
- 本地模拟不连接瑞成云或 Turnstile，不产生短信费用，也不代表生产网络与供应商验收通过。

## 尚未完成，禁止据此上线

1. 在真实 MySQL 8/RDS TLS 环境执行迁移、事务和并发限流测试；
2. 重置已经暴露的短信密码，配置固定出口 IP、白名单并用专用号码小流量验证；
3. 配置真实 Turnstile 站点密钥并验证 hostname/action；
4. 接入监护人同意服务；当前生产工厂对未成年人失败关闭；
5. 通过显式外发授权完成 Luna、Terra、Sol 三层只读复核；
6. 完成枚举时序、WAF/SLB 可信代理、告警、成本熔断、备份和回滚演练；
7. 由人工交付经理记录部署批准。
