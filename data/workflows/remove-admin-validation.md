# 移除后台管理系统

日期：2026-08-31。用户要求删除本地 `/admin` 后台管理系统代码。

## 范围与边界

- 删除后台 HTML/JS、专用 CSS、`services/web_ops` 查询和角色服务、ASGI 装配、`grant-admin` 命令与 OpenAPI 运营接口。
- 旧后台页面、静态文件、API 在已登录与未登录状态均返回 404；不改变认证状态机。
- 保留会话账号绑定、Token 实际计量、题库校验、学习用量、学生端页面与现有安全审计。
- 不删除数据库记录、不执行迁移或 DROP TABLE。已执行的 0012 历史迁移和 smoke 的外键清理兼容保留，不再提供后台运行时。
- 删除的源码均已存在于 Git 历史，可恢复。无需引入新依赖。
- 本地测试通过后重启已识别的 Web/Harness 进程使删除生效，不停止 MySQL。

## 验证

- `python -X utf8 -B -m unittest discover -s tests -p "test_*.py"`：282 项通过（删除 3 项已移除运营服务的测试，将旧后台页面/API 测试改为删除回归测试）。
- 路由回归覆盖已登录/未登录 GET/POST 的旧后台页面、静态文件和接口均返回 404；学生工作台的登录隔离及 Harness 会话归属、Token 计量测试仍通过。
- 重启 Web/Harness 后实际 HTTP 检查：`/admin`、`/admin/`、`/web/admin.js`、`/web/admin.html`、`/v1/admin/dashboard` 均为 404；`/healthz`、`/login`、`/errors`、`/practice`、`/progress` 和 Harness UI 为 200。没有进行真实判题或生成 PDF。
- `local_env.py status` 全部检查通过；帮助菜单不再包含 `grant-admin`。
- `git diff --check` 通过（仅提示 LF/CRLF 转换）；实际无数据库迁移或清表操作。

## 发布边界

不调用外发模型审查；没有新增项目材料外发授权，不重试此前拒绝的外发方式。此任务为本地移除功能，不代表独立安全审批或生产部署完成。
