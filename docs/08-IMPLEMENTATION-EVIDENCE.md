# v0.3.2 实施证据

日期：2026-08-23

## 已完成并提交

| 范围 | 证据 | 提交 |
|---|---|---|
| 架构与 API 契约 | `docs/03-TECHNICAL-ARCHITECTURE.md`、`docs/12-ARCHITECTURE-DATA-API-CONTRACT.md`、`openapi/web-v1.json` | `f58eb87` |
| 手机号直接注册/登录 | `services/web_auth/registration.py`、`asgi.py`；验证请求无资料、年龄、身份或监护字段 | `44c8933` |
| 会话 | 当前会话查询、退出当前设备、全端退出；Cookie 保持 Secure/HttpOnly/SameSite=Lax | `44c8933` |
| 账号前向修订 | 不改 0001；`0003_account_simplification.sql` 移除资料列和监护表 | `44c8933` |
| `user_id` 领域数据 | 重构 0002；文件、识别、任务、作答、判题候选、正式错题、推荐、复习直接归属 `user_id` | `44c8933` |
| 文件入口 | 文件名、大小、魔数、DOCX zip 限制、SHA-256、用户隔离且不可猜对象键 | `44c8933` |
| 首题 HTTP 闭环 | 手机验证→上传→识别候选→修订/确认→判题候选→确认入本→列表/详情 | `44c8933` |
| 响应式前端壳 | 正式 Logo、手机号入口、桌面侧栏、移动底栏、工作台、上传和状态反馈 | `44c8933` |
| 整卷候选编辑 | 逐题修订、拆分、合并、排除、版本确认 | `44c8933` |
| 题库迁移工具 | 仅通过权威 `notebook.py` 读取，dry-run 默认，显式权利确认后才写 MySQL，哈希对账与幂等键 | `44c8933` |
| 判题模型路由 | Terra 产生只读候选，数学不确定性升 Sol；外发显式授权；冻结输入且不含 `user_id`/手机号 | `67ed05b` |
| 候选/正式质量门 | `input_version` 重检、`unclear` 禁止入本、正式提交幂等、跨用户统一 404 | `44c8933`、`67ed05b` |

## 自动化证据

- `python -X utf8 -B -m unittest discover -s tests -p "test_*.py"`：75 项通过。
- OpenAPI 和 `schemas/grade-candidate.schema.json` 均由 Python 标准库成功解析。
- HTTP E2E 覆盖两个账号：用户 B 访问用户 A 的错题返回 404。
- 文件负向测试覆盖路径穿越、伪扩展名、空/超限、损坏 DOCX 和跨用户相同内容。
- 迁移账本测试覆盖按顺序执行、失败不记账、同名异哈希失败关闭。
- `WEB-PRD-003` 已完成 `architecture_and_contract`、`identity_and_sms`、`domain_data`、`file_pipeline`、`recoverable_jobs`、`codex_routing`、`quality_gates`；`sqlite_migration` 保持进行中。

## 本地环境现状

- Oracle MySQL 8 本地实例运行于 `127.0.0.1:3307`，所需 Python 依赖已安装。
- 只读审计显示当前库只有旧认证表；`web_users=0`、`guardian_consents=0`，没有个人业务数据。
- `scripts/local_env.py doctor` 除 `schema_ready=false` 外全部通过。
- 执行 0002/0003 的真实 DDL 因包含删除旧表/列而被安全门拒绝；未绕过、未改变数据或 Schema。
- 技能绑定的当前题库报告 0 道题；迁移工具与映射测试已完成，但没有可导入的正式题目。

## 未完成门禁

1. 用户明确批准后，备份或重建当前空的本地 MySQL 库，再执行 0001→0002→0003、重复迁移和真实 HTTP smoke。
2. 对实际权威桌面题库运行两次 dry-run；仅在来源权利确认、总数和 SHA-256 一致后导入。
3. 连接真实解析/判题 Worker，完成固定数学基准集与人工复核包；当前测试使用确定性候选注入验证提交门。
4. 完成推荐、分阶段复习、练习 PDF、导出/注销、告警、备份恢复、性能和独立系统安全复核。
5. 瑞成云、Turnstile、RDS/OSS、固定出口 IP 和阿里云部署继续后置，最终部署必须人工批准。

上述门禁完成前，当前代码只能视为本地功能切片，不能上线。
