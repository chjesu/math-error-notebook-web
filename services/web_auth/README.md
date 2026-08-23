# Web 手机验证码认证内核

> 产品差异：本目录当前实现 v0.4.0 本地候选，提供独立验证码登录和手机号注册；注册增加密码与协议确认。当前实现不包含资料、昵称、年级、身份、家庭或监护门；真实短信/CAPTCHA、生产密钥托管和部署仍不在本地候选范围，详细标准见 `docs/13-LOGIN-REGISTER-PRD.md`。

本目录是多用户 Web 版批次 1 的确定性安全内核，不调用大模型。它已经实现并测试：

- 中国大陆手机号规范化；
- 6 位随机验证码、5 分钟有效、HMAC 存储、错误次数锁定和单次消费；
- 新验证码使旧验证码失效；
- 手机号、IP、设备和全局预算的原子限流参考实现；
- 风险升高后要求服务端验证的一次性 CAPTCHA；
- 短信供应商失败的安全终态；
- 验证成功后创建最小账号或复用已有账号，不要求资料、身份、家庭或监护信息；
- 会话令牌只在成功响应中返回，服务端仅保存哈希；
- 手机号、IP、设备和验证码不以明文写入审计。
- `POST /v1/auth/otp/request` 与 `POST /v1/auth/otp/verify` 的 ASGI 接口适配器；
- HTTPS/Host/JSON/请求体边界、安全 Cookie、统一错误响应，并明确不信任客户端 `X-Forwarded-For`。

`registration.py` 不依赖 Web 框架或数据库驱动，`asgi.py` 只实现最小 ASGI 协议，`mysql_store.py` 接受任意 PyMySQL 兼容的连接工厂。`bootstrap.py:create_app` 从服务器环境变量装配 MySQL、瑞成云和 Turnstile，并拒绝远程明文 MySQL；不得使用 `RecordingSmsSender`、`InMemoryCaptchaVerifier` 或 `InMemoryRegistrationStore` 运行生产服务。数据库骨架见不可改写的 `migrations/0001_phone_registration.sql`，账号简化由 `migrations/0003_account_simplification.sql` 前向完成。

## 生产启动

先通过服务器密钥配置注入下列变量，值不得写入仓库或命令历史：

```text
LZLM_ALLOWED_HOSTS=app.example.cn
LZLM_AUTH_PEPPER_B64=<至少32随机字节的Base64>
LZLM_MYSQL_HOST=<RDS内网地址>
LZLM_MYSQL_PORT=3306
LZLM_MYSQL_USER=<低权限账号>
LZLM_MYSQL_PASSWORD=<密钥配置引用>
LZLM_MYSQL_DATABASE=<数据库名>
LZLM_MYSQL_SSL_CA=<RDS CA证书绝对路径>
RUICHENG_SMS_USERNAME=<密钥配置引用>
RUICHENG_SMS_PASSWORD=<密钥配置引用>
RUICHENG_SMS_SIGNATURE=【已报备签名】
TURNSTILE_SECRET_KEY=<密钥配置引用>
TURNSTILE_EXPECTED_ACTION=otp_request
```

安装 `requirements-web.txt` 后，以工厂模式启动：

```powershell
uvicorn services.web_auth.bootstrap:create_app --factory --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips=<SLB内网IP>
```

SLB/WAF 负责公网 HTTPS，且只有列入 `--forwarded-allow-ips` 的代理能够改写 ASGI 客户端 IP 和协议。应用提供 `GET /healthz` 作为进程存活检查；该端点不证明 MySQL、Turnstile或短信通道就绪。

当前本地工厂使用 v0.4.0 四接口契约；所有认证状态机修改必须集中在权威注册状态机中，保持 `purpose=login|register` 隔离、密码凭据、协议记录、目标 API、负向测试和前向迁移。生产工厂仍须单独完成真实供应商、密钥托管、观测和部署验收。

生产接入仍须完成`docs/05-TEST-ACCEPTANCE-OPERATIONS.md`中的真实MySQL并发、供应商回执、账号存在性批量滥用、预算熔断、密码安全、监控和灰度验收。当前脚本化测试不能替代RDS集成测试。模型只能离线审查代码，不能实时决定是否发送验证码、是否登录、是否注册或是否授予会话。
