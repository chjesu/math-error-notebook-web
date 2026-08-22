# Web 手机验证码注册内核

本目录是多用户 Web 版批次 1 的确定性安全内核，不调用大模型。它已经实现并测试：

- 中国大陆手机号规范化；
- 6 位随机验证码、5 分钟有效、HMAC 存储、错误次数锁定和单次消费；
- 新验证码使旧验证码失效；
- 手机号、IP、设备和全局预算的原子限流参考实现；
- 风险升高后要求服务端验证的一次性 CAPTCHA；
- 短信供应商失败的安全终态；
- 未成年人必须提交经服务端验证的监护同意凭据；
- 会话令牌只在成功响应中返回，服务端仅保存哈希；
- 手机号、IP、设备和验证码不以明文写入审计。
- `POST /v1/auth/otp/request` 与 `POST /v1/auth/otp/verify` 的 ASGI 接口适配器；
- HTTPS/Host/JSON/请求体边界、安全 Cookie、统一错误响应，并明确不信任客户端 `X-Forwarded-For`。

`registration.py` 不依赖 Web 框架或数据库驱动，`asgi.py` 只实现最小 ASGI 协议，`mysql_store.py` 接受任意 PyMySQL 兼容的连接工厂。`bootstrap.py:create_app` 从服务器环境变量装配 MySQL、瑞成云和 Turnstile，并拒绝远程明文 MySQL；不得使用 `RecordingSmsSender`、`InMemoryCaptchaVerifier`、`InMemoryGuardianConsentVerifier` 或 `InMemoryRegistrationStore` 运行生产服务。数据库骨架见 `migrations/0001_phone_registration.sql`。

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

监护人同意服务尚未接入时，生产工厂使用失败关闭验证器：成年人可正常注册，未成年人不能伪造回执绕过限制。完成监护人流程后再替换该适配器。

生产接入仍须完成 `docs/web/05-TEST-ACCEPTANCE-OPERATIONS.md` 中的真实 MySQL 并发、供应商回执、枚举时序、预算熔断、监控和灰度验收。当前脚本化测试只证明事务 SQL 的锁定顺序和提交/回滚路径，不能替代 RDS 集成测试。模型只能离线审查代码，不能实时决定是否发送验证码、是否登录或是否授予会话。
