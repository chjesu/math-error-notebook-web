# 李兆霖数学错题本 Web 项目规则

- 所有文本固定 UTF-8；Python 使用 `python -X utf8 -B`。
- 禁止把短信、MySQL、Turnstile、KMS 或会话密钥写入代码、测试、文档、日志和 Git。
- 修改前先读 `PROJECT_ARCHITECTURE.md`；注册状态机只在 `services/web_auth/registration.py`，不得建立平行认证实现。
- 在线认证、限流、权限和验证码判断必须是确定性代码；大模型只能做离线只读审查，不能决定是否发短信、登录或部署。
- 长任务必须用 `scripts/project_workflow.py` 创建清单、领取步骤、附证据并恢复；高风险步骤的实现者不能成为唯一批准人。
- 模型任务必须走 `scripts/codex_task_router.py`：需求 Luna、实现 Terra、安全 Sol；外发前必须使用显式授权参数。
- 生产只使用 MySQL 8；数学题库和用户上传文件不进入本仓库。
- 供应商只支持明文 HTTP：生产必须固定出口 IP、服务商白名单、出站安全组和禁请求体日志。超时不得自动重发。
- 每次代码修改必须运行 `python -X utf8 -B -m unittest discover -s tests -p "test_*.py"`，并提交 Git。

