# 工作台 Codex Harness 集成基线

## 1. 决策

工作台不再自行模拟 Codex 会话循环。首轮图片识别、题干与作答修正、判题和追问直接复用官方开源 Codex `app-server` 协议；`codex exec` 只保留给无用户会话的批量工程审查。工作台的权威运行时入口为 `scripts/codex_app_server.py`，业务仍统一经 `scripts/codex_task_router.py` 选择模型和 Schema。

这里的“拥有 Harness 能力”是指复用同一上游线程、回合、条目和事件运行时，而不是复制 Codex 桌面界面，也不是把开发者电脑权限交给普通用户。

## 2. 当前已接通

- `initialize` / `initialized` 标准握手；
- `thread/start` 创建可持久化线程，`thread/resume` 跨 Web 服务进程恢复同一线程；
- `turn/start`、结构化 `outputSchema`、Sol 路由和多轮上下文；
- `localImage` 首轮图片输入；首次识别、判题和后续交互恢复同一 thread；
- `turn/started`、`item/started`、`item/agentMessage/delta`、`item/completed`、token 使用、上下文压缩和 `turn/completed` 真实通知；
- `POST /v1/intakes/{intake_id}/chat-turn-stream` NDJSON 流，页面按真实事件更新“正在连接、理解、分析、组织回复、整理上下文”；
- `codex_conversations` MySQL 映射，浏览器永远看不到或提交上游 thread id；
- 实验能力握手后的 `thread/items/list` 使用官方不透明游标分页读取持久线程；浏览器只收到绑定当前用户与 intake 的产品游标，以及真实用户输入和助手答复；
- `turn/interrupt` 已接到输入框运行态的停止按钮；停止只终止当前回合，不确认候选、不写入错题本；
- `thread/compact/start` 已接到“整理上下文”，完成状态来自 `thread/compacted` 或 `contextCompaction` 条目；
- 最终回复继续通过 `math-loop-turn.schema.json`，题干确认、判题确认和入本仍由服务端资源归属、版本和幂等门决定。

## 3. 错题本安全配置

普通工作台线程固定为只读 sandbox、`approvalPolicy=never`，并关闭 shell tool 与用户 MCP 配置入口。即使上游 Harness 支持命令、文件修改、MCP 和审批，学生工作台也不开放这些能力。任何意外服务端审批请求都默认拒绝。

账号认证、验证码、权限、限流、用户隔离、确认口令和正式写库不由模型决定。手机号、验证码、密码、会话令牌、数据库和短信配置不得进入线程、事件或日志。

## 4. 能力状态

| 能力 | 当前状态 |
|---|---|
| 多轮线程与跨服务重启恢复 | 已接通；MySQL 保存不透明 thread id |
| 真实增量事件 | 已接通；NDJSON 推送到工作台 |
| 上下文压缩 | 已接通 `thread/compact/start`、完成事件和工作台按钮 |
| 结构化业务候选 | 已接通；Schema + 确定性写入门 |
| 历史消息跨页面恢复与分页 | 已接通 `thread/items/list`；每页最多 20 个上游条目，使用服务端绑定的产品游标继续加载更早历史 |
| 停止当前回合 | 已接通 `turn/interrupt`；输入框发送键在运行中变为停止键 |
| 运行中追加指令、重新生成、分叉 | 待产品数据模型；分叉不能覆盖 intake 当前 thread 映射，须先定义一题多分支的归属、展示和入本规则 |
| Shell、文件修改、MCP、插件、任意技能 | 工作台明确禁用；不属于学生错题流程 |
| 首次图片识别与判题预处理也并入同一线程 | 已接通；图片使用 `localImage`，识别与判题使用 Sol 高推理档 |

因此，当前版本已经从“仿 Harness 的页面”切换到“官方 Harness 驱动的连续会话核心”，历史翻页、停止和主动压缩已有真实控制面；运行中追加、重新生成和分叉仍不能写成已完成。

## 5. 验收标准

1. 两个独立 app-server 进程用同一个 thread id 连续两轮，第二轮能使用第一轮上下文。
2. 页面收到的进度来自 app-server 通知，不使用定时器伪造模型阶段。
3. Web 服务重启后，服务端能从 MySQL 找到当前用户和 intake 对应的 thread id；客户端不能替换它。
4. 跨用户访问仍返回通用不存在，不能获取另一用户的线程或候选。
5. 模型失败时不写候选或正式错题，事件和审计不记录题目正文、提示词、图片路径或秘密。
6. “加载更早”必须携带服务端返回的产品游标，游标不能用于读取其他用户的 thread。
7. 停止按钮必须发出 `turn/interrupt`；压缩按钮必须发出 `thread/compact/start`，不能用前端动画或直接杀进程冒充。
