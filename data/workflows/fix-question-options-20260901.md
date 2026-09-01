# 推荐题选择题选项缺失修复 + 2970 今日 PDF 重生成

日期：2026-09-01。触发：用户反馈"今天生成的 pdf 中，有推荐题的选择题没有选项"。

## 根因

- 桌面版权威题库（`..\math-error-notebook\data\math_notebook.db`，141MB）中选择题带 `options_json`（4941 道）。
- Web 迁移脚本 `scripts/migrate_question_bank.py` 的 `map_question` 把 `options` 计入内容哈希，但写入 `question_versions` 时只存 stem/answer/solution，**选项被丢弃**（表本身无 options 字段）。
- 后果：Web 题库 4947 道选择题（答案 A-D）中仅 105 道题干内嵌 "A."，其余无选项；PDF/前端推荐题均不带选项。

## 修复内容

1. **迁移** `services/web_domain/migrations/0014_question_options.sql`：`ALTER TABLE question_versions ADD COLUMN options_json JSON NULL AFTER solution_text`；已注册进 `scripts/local_env.py` MIGRATIONS 并应用（`python -m scripts.local_env migrate`，台账含 0014）。
2. **模型** `services/web_domain/learning.py`：`Question` 增加 `options: tuple[str, ...] | None = None`（末尾带默认值，不破坏既有位置构造）。
3. **存储** `services/web_domain/mysql_store.py`：
   - 新增静态助手 `_options()` 解析 JSON 数组 → tuple。
   - `assign_recommendations` 与 `list_recommendations` 的 SELECT 增加 `v.options_json`，构造 `Question(... options=...)`。
   - `practice_items` 推荐题 item 增加 `"options": question.options`。
4. **导入** `scripts/migrate_question_bank.py`：`map_question` 返回 `options_json`，`INSERT ... question_versions` 增列并 ON DUPLICATE 更新（后续导入不再丢选项）。
5. **回填** 新增 `scripts/backfill_question_options.py`：按桌面指纹（`questions.canonical_sha256` = 桌面 `fingerprint`，映射 100% 覆盖 4941 道）回填存量 `options_json`；已执行，更新 4941 行。
6. **PDF 渲染** `services/web_domain/practice_pdf.py`：推荐题题干后若含 options，输出「选项：A．…　B．…　C．…　D．…」。
7. **API/前端** `services/web_app/asgi.py` `_recommendation` 增加 `options`；`web/app.js` 练习清单渲染选项；`web/app.css` 加 `.recommendation-options` 样式。
8. **测试** `tests/test_practice_pdf.py` 新增 `test_recommendation_options_are_printed_in_the_pdf`（pypdf 提取文本断言 A-D 出现）；`tests/test_web_domain.py` 新增 `QuestionOptionParsingTests`；`tests/test_local_env.py` 迁移列表补 0014。

## 今日 PDF 重生成（2970）

- 旧 job `2fa09ba4cef545cbbd331c41cb4f4e19`（10:32 生成，修复前，选择题无选项）→ 先备份至 `data/runtime/regenerate-pdf-2970-20260901/backup.json`（job+file 行，Git 忽略），再单事务 DELETE `web_jobs` 该行（1 行，含 file 引用保留旧文件记录与磁盘 PDF）。
- 以相同参数（error_ids 7 道、include_answers=False、plan_kind=daily_review）通过 `NotebookService.create_practice_pdf` 重新生成：新 job `9496d35ff71a4580869e7bc696780cf4`（completed）、新 file `fa4c6437e8164d54b7eb7355873de37f`（practice PDF，343240 B，5 页），question_count 7、gap 0。
- 验证：新 PDF 用 pypdf 提取文本，含「选项」「A．」「B．」「C．」「D．」「双曲线」；今日 completed practice_pdf 仅此 1 份，为修复后计划。
- Web 服务已重启加载新代码（原 serve 进程 63576/56108 → 新进程 64364/65692），根路径 HTTP 200。
- 未调用外发模型（PDF 生成本地确定性链路），未停止 MySQL。

## 验证结果

- `.\.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -p "test_*.py"`：293 个测试全部通过。
- 端到端：`practice_items` 推荐题 7 道中 3 道选择题均带 options（`0a6af0c9...` 双曲线题选项 `["A．$-3$","B．$-\frac{1}{3}$","C．3","D．$\frac{1}{3}$"]`），其余 4 道为填空/解答题本无选项。
- 回填覆盖：desktop 4941 道带选项，Web 侧 4941 道 `options_json` 已写入；57 道 A-D 答案题在桌面无选项，保持无选项（数据本身缺失，非丢失）。

## 遗留说明

- Web 前端错题详情页 `/v1/errors/{id}/recommendations` 已含 options 字段并渲染（同根因一并修复）。
- 桌面题库中 57 道答案字母但无 options_json 的题目（如部分判断题），PDF 仍不显示选项，属源数据缺失。
