# 2970 今日"生成今日复习推荐题 PDF"次数重置

用户授权：本轮明确要求"重置他的生成pdf次数"（他 = 账号 2970，手机尾号 2970）。
范围：账号 2970 在中国时间 2026-09-01 已固定的每日复习（daily_review）PDF 计划。

## 只读盘点

- 今天唯一完成的 `practice_pdf` job：`1273527f655146659fec0a89d056cc52`
  - user_id `f3c8f227ad934688a31dc9534dfcf596`（2970），plan_kind `daily_review`
  - generated_at `2026-09-01T09:42:40.212421+00:00`，7 道题，14 条复习清单（原题+推荐）
  - 关联 file `a7b0ca32fa6b487ea60c53a7fbc734d9`（practice_pdf / ready / practice-1273527f.pdf，333259 B）
- 账号 2970 历史 completed `practice_pdf` 共 64 份（含 08-31 两份、08-30 清理先例等），均不在本次范围。
- 今日 daily_learning_usage：账号 2970 无 09-01 推荐题/判题计数，无需清理用量。
- 复习任务（review_tasks）按错题独立存在，与 PDF job 无级联，删除后复习阶段不变。

## 执行边界

- 未停止运行中的 Web 服务（8000/python.exe）：`list_practice_pdfs` 每次请求实时查 MySQL（store 直连），单行事务删除即刻生效，无内存缓存；期间前端按钮因 fixedPlan 禁用，不会并发新生成。
- 删除前把 job 全行 + file 记录导出为 JSON 备份至 `data/runtime/reset-pdf-2970-20260901/backup.json`（Git 忽略，不入库），可恢复。
- 单事务：`DELETE FROM web_jobs WHERE id=1273527f... AND user_id=2970 AND job_type='practice_pdf' AND status='completed'`，影响行数 1，提交成功。
- 保留 `web_files` 记录与磁盘 PDF（与 08-30 `cleanup_pdf_jobs_2970_20260830` 先例一致）。
- 未修改产品代码，未调用外发模型，未伪造独立批准人。

## 验证结果

- 重置后账号 2970 今日 completed `practice_pdf` = 0，`today_practice_plan` 返回 None（已用与 store 相同的 SQL/筛选复算确认）。
- 历史 64 份 PDF 及文件记录保留；Web 健康页 HTTP 200。
- 效果：错题本/学习进度页"生成今日复习推荐题 PDF"按钮解除"今日计划已固定"禁用，可再次生成（受当日推荐题额度约束）。
