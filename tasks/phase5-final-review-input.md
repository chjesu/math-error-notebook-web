# Phase 5 最终检查输入

## 目标

检查当前分支相对 `HEAD` 的未提交差异是否完整实现 Phase 5：标准错因筛选、用户学习画像、难度递进推荐，以及 `review` / `self_test` 两种练习 PDF。

## 冻结不变量

- 错因属于学生本次作答关联的错题，不是题目的永久标签。
- 标准错因与知识点只来自已校验诊断 JSON；缺失或无效时不猜测。
- 所有列表、画像、任务和下载继续由服务端会话注入 `user_id` 并按用户过滤。
- 候选推荐只能来自已验证且开放或用户授权的题库，排除已做题和原题。
- 推荐先按题干共同上下文，再优先高于原题且增幅最小的难度。
- 自测卷只能包含题目和作答留白，不得泄漏错因、理由、解析或答案。
- 复习卷包含错因、知识点、正确过程和参考答案。
- 新任务幂等摘要绑定规范化 `mode`；旧 `include_answers` 请求和历史记录保持可读，冲突输入失败关闭。
- MySQL 视图使用 `SQL SECURITY INVOKER`；应用查询仍必须按 `user_id` 限定，筛选值必须参数化。

## 必读证据

- `tasks/phase5-plan.md`
- `docs/decisions/004-learning-profile-and-practice-modes.md`
- 当前 `git diff HEAD --` 的全部差异
- `services/web_domain/migrations/0013_learning_profile_views.sql`
- `openapi/web-v1.json`

## 已运行验证

- 101 个 Phase 5 聚焦用例通过：学习领域、PDF、MySQL 查询契约、本地迁移列表、OpenAPI、前端、隐私，以及一条完整 HTTP 闭环。
- 变更 Python 文件 AST 解析通过。
- `web/app.js` JavaScript 语法检查通过。
- OpenAPI JSON 解析通过。
- `git diff --check` 通过。
- 按用户指示未运行全量测试。

## 输出要求

只读检查，不修改文件。按正确性、架构、安全、兼容、性能和测试有效性检查。仅报告有证据的问题，注明严重级别和文件位置；若无阻断项，明确给出可提交结论。不要建议扩大到 Phase 6、生产部署或全量测试。
