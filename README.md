<p align="center">
  <img src="assets/branding/lizhaolin-math-notebook-logo-concept-v1.png" width="520" alt="李兆霖数学错题本">
</p>

# 李兆霖数学错题本 Web

> **教育同权，让每个孩子都能获得高质量、可持续的个性化学习支持。**

这不是一个单纯“保存错题”的文件夹，也不是一个自动给答案的工具。它是一套面向高中数学学习的智能错题本：从学生真实作答出发，识别题目与手写步骤，定位第一处实质错误，分析错因，再从经过验证的题库中安排针对性训练，通过持续复习追踪掌握情况。

Web 版延续 Skill 版的学习理念，把**看懂学生思路、诊断错误根源、安排恰当练习、持续追踪掌握情况**的流程放进浏览器。学生通过会话上传照片、补充说明和订正，在错题本、练习 PDF 和学习进度页面查看记录；日常使用不需要安装 Skill 或操作命令行。

## 项目起因

传统错题本往往解决了“把错题记下来”，却没有完整解决后面的学习问题：

- 知道答案错了，却不知道思路从哪一步开始偏离；
- 错因被简单归结为“粗心”，知识缺口、方法误用和分类遗漏没有被区分；
- 同类练习依赖临时找题，难度与质量难以稳定控制；
- 订正一次后缺少追踪，无法判断是否已经稳定掌握。

李兆霖数学错题本由真实的高中数学学习需求逐步发展而来。它希望回答一个朴素的问题：**能否把一次错误转化为一条完整、可靠、能够持续执行的学习路径？**

因此，系统关注的不只是收集了多少题，而是学生是否理解了错误、完成了适量训练，并在后续复习中独立做对。

## 核心原理

### 从订正一次，到持续主动回忆

项目借鉴遗忘曲线与间隔复习的思路，将复习安排为可追踪的六个阶段。每次先遮住答案独立重做，再核对并及时订正；后续安排依据实际作答结果和完成时间调整，不把浏览答案或点击“已查看”视为掌握。

### 以真实作答为证据，形成学习闭环

```mermaid
flowchart LR
    A[上传题目与手写作答] --> B[识别与判题]
    B --> C[定位错误与整理错因]
    C --> D[匹配已验证练习题]
    D --> E[生成练习 PDF]
    E --> F[独立作答与拍照订正]
    F --> G[记录结果与安排复习]
    G --> D
```

- **看过程，不只看答案**：结合题干、图形和解题步骤判断；内容不清或证据冲突时保留待确认状态。
- **练习有依据**：推荐题来自已验证且具备使用授权的题库，保留来源和推荐理由；不足时明确显示缺口。
- **复习有记录**：将 PDF 题目、作答结果和原错题的复习任务关联，持续展示待复习、需改错及完成情况。
- **模型与程序分工**：模型负责图片理解与数学推理；程序校验账号归属、输入版本和写入条件，保存可核对的处理回执。

## 项目价值

- **对学生**：明确“错在哪里、为什么错、下一步做什么”，减少无目的刷题，逐步认识自己的薄弱环节。
- **对家长和教师**：把零散照片、订正与复习结果连接成可追踪的学习记录，将精力留给需要讲解和沟通的部分。
- **对教育资源公平**：降低获得持续、个性化反馈的使用门槛，让日常错题处理不再完全依赖临时的一对一辅导。

## Web 版与 Skill 版

两者共享学习理念，但不是共用数据库的两个入口。Skill 版通过智能体和命令行操作本地学习资料；Web 版提供浏览器会话与学习页面，使用独立 MySQL，并由服务端按登录账号隔离数据。题库只能通过受控迁移接入，Web 不直接读取或共享 Skill 版的 SQLite 主库。

本地部署不等于离线判题：启用外部模型后，处理所需的题目图片预览及题干、作答等材料会发送给所配置的模型服务。密钥由部署端管理，不放入浏览器或仓库。

## 它不是什么

- 不是替代教师判断的“全自动老师”，模型判题仍可能出错，需要复核与纠正；
- 不是未经验证便批量生成推荐题的题海工具；
- 不是绕过版权、登录或付费限制的试题抓取器；
- 不是已经完成生产安全验收的在线服务，当前仍以本地开发和验证为主。

## 开发状态与数据边界

个人数学错题本的独立 Web 项目。v0.4.0 本地候选冻结四个认证接口：登录验证码申请/验证、注册验证码申请、注册完成；注册保存密码和协议版本并自动登录。用户不填写昵称、年级或其他资料，不选择身份、不创建家庭，也不经过监护或实名流程。既有本地学习闭环的验收记录见下方文档，后续变更仍需重新验证，生产门禁尚未完成。2026-08-31 按用户要求删除运营后台页面、专用接口、查询服务与授权命令，旧后台地址返回 404；学生端、会话计量和现有数据库记录保留。

本仓库不包含正式数学题库语料、错题照片或短信密钥。题库只能通过受控迁移接入；本地没有合格推荐题时会明确显示缺口。

## 快速检查

```powershell
python -X utf8 -B scripts/project_workflow.py doctor --json
python -X utf8 -B -m unittest discover -s tests -p "test_*.py"
python -X utf8 -B scripts/codex_task_router.py route --task web-security-review --json
Get-Content -Raw -Encoding UTF8 data/workflows/local-candidate-20260901.md
```

当前可审计结果见 [`data/workflows/local-candidate-20260901.md`](data/workflows/local-candidate-20260901.md)；旧 `WEB-PRD-003` 工作流证据已过时，不作为当前候选完成证据。

## 本地模拟环境

本地环境使用真实 MySQL 8，但短信和 CAPTCHA 均为模拟实现，不连接供应商、不产生费用：

本地 C/S 模拟只使用一个浏览器入口：`http://127.0.0.1:8000/`。页面、会话、业务 API、PDF 下载和 WebSocket 同源；Harness 与 MySQL 留在内部回环网络。前端不写死其他端口，后台启动会校验插件并等待健康检查。这不等于 ECS 生产部署；模型判题仍可能产生所配置供应商的调用费用。

### 一键启停服务（推荐）

项目已内置跨平台便捷脚本，自动识别环境、加载 `.env` 模型配置并支持后台守护运行：

- **Windows 10 / 11**：
  - 前台启动：`.\start.bat` 或 `powershell -ExecutionPolicy Bypass -File scripts\start.ps1`
  - 后台守护启动：`.\start.bat -Daemon`
  - 一键停止：`.\stop.bat` 或 `powershell -ExecutionPolicy Bypass -File scripts\stop.ps1`
- **Linux / macOS / WSL**：
  - 前台启动：`./start.sh`
  - 后台守护启动：`./start.sh --daemon`
  - 一键停止：`./stop.sh`

详细使用参数、端口管理与 Systemd 扩展见 [跨平台服务启停与运维脚本手册](docs/23-SERVICE-MANAGEMENT-SCRIPTS.md)。

### 新电脑初始化

新电脑可直接执行以下命令完成依赖安装、空白数据初始化、smoke 和启动：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\bootstrap_local.ps1
```

完整前置资源、空白数据范围和可选题库导入见 [新电脑本地初始化](docs/19-NEW-COMPUTER-BOOTSTRAP.md)。

```powershell
.\.venv\Scripts\python.exe -X utf8 -B scripts\local_env.py init
.\.venv\Scripts\python.exe -X utf8 -B scripts\local_env.py smoke
.\.venv\Scripts\python.exe -X utf8 -B scripts\local_env.py serve --enable-harness-model --enable-harness-ui
```

服务仅监听 `127.0.0.1:8000`。请求验证码后，页面会明确标记“仅限本地测试”并自动填入模拟验证码；正式服务响应不会返回验证码。测试 CAPTCHA token 为 `local-captcha`。停止数据库使用：

默认不向外部模型发送材料。当前用户明确授权本地测试时，可使用 `python -X utf8 -B scripts/local_env.py serve --enable-harness-model --enable-harness-ui` 同时启用固定版 DeepSeek Harness 核心和官方 Web 前端。工作台直接装载 `@deepseek-ai/dsh-web-frontend@0.1.1-rc.2`，不是仿写页面；会话、历史分页、附件、上下文计量、自动压缩、工作区、设置和输入交互均由官方 Host 提供。供应商、网关、协议、模型、密钥环境变量名和文本/图片能力均可配置；切换到 Qwen 等 OpenAI 兼容通道不复制错题流程。旧的 `--enable-codex-model` 仅保留作对照。PNG/JPEG 先生成识别候选，上传后系统按顺序自动冻结版本并判题；正确题自动跳过，错误或部分正确在确定性版本门和写库门校验后自动入本，无法识别或证据冲突时停留等待补充。外发前复验上传哈希，并仅发送去元数据、限尺寸的重编码预览图。

```powershell
.\.venv\Scripts\python.exe -X utf8 -B scripts\local_env.py stop
```

随机本地密码、数据文件和日志只保存在 Git 忽略的 `.runtime/local-mysql/`，禁止复制到生产环境。

需要模型审查时，先用 `scripts/prepare_review_packet.py --file <显式文件>` 生成冻结、限长、密钥检查过的 JSON，再在确认外发内容后运行路由器的 `run --authorize-external-send`。

## 后续生产部署（当前不执行）

当前 localhost + MySQL 8 的个人错题本支持：上传→可配置 Harness 批量识别候选→系统自动逐题冻结版本→独立解题与判题复核→正确题跳过、错误或部分正确自动入本→仅已验证推荐→复习→PDF；同一 Harness 会话支持自然语言修正和追问。每个账号按中国标准时间显示今日学习负荷：判题建议 24 道、上限 40 道，推荐题建议 12 道、上限 24 道，可完整覆盖一套 21 道试卷并保留订正余量；失败、无法识别、重试和重复提交不重复计数，同一批图片不会处理到一半被截断。工作台接收真实运行时事件，线程映射可跨 Web 服务重启恢复，刷新后会恢复最近一次会话的产品消息，无法识别、证据冲突或模型不可用时安全停止。导出为业务 JSON+文件元数据（不含上传原始二进制，最多下载 3 次并审计），注销执行停用、业务失效和对象文件删除，认证/协议/审计按策略留存。独立运营后台已于 2026-08-31 删除，旧 `/admin` 与 `/v1/admin/*` 返回 404。运行中追加/分叉控制、生产异步 Worker、PDF/DOCX 自动解析、真实短信/CAPTCHA/KMS/OSS、PWA、人工复核提交、敏感访问审批、危险批量操作、压测/灾备/观测、正式部署和生产恢复均延期，不得写成完成。

1. 按 [`docs/22-ALIYUN-DEPLOYMENT-RUNBOOK.md`](docs/22-ALIYUN-DEPLOYMENT-RUNBOOK.md) 的 0001→0013 顺序执行完整迁移并校验 SHA-256 账本；不能只执行 0001。
2. 从密钥管理服务注入 `services/web_auth/README.md` 列出的环境变量。
3. 安装锁定并审查后的生产依赖。
4. 冻结完整 `NotebookAsgiApp` 的生产装配、可信代理和 Worker 启动入口；当前 `local_env.py serve` 与认证子应用启动器都不得直接用于生产。

上线前必须完成 `docs/05-TEST-ACCEPTANCE-OPERATIONS.md` 的真实 MySQL 并发、短信小流量、枚举时序、成本熔断和回滚验收。

## 文档

- [项目架构](PROJECT_ARCHITECTURE.md)
- [产品需求](docs/02-PRODUCT-REQUIREMENTS.md)
- [产品功能设计](docs/09-PRODUCT-FUNCTION-DESIGN.md)
- [技术架构](docs/03-TECHNICAL-ARCHITECTURE.md)
- [实施计划](docs/04-IMPLEMENTATION-PLAN.md)
- [测试与运维](docs/05-TEST-ACCEPTANCE-OPERATIONS.md)
- [角色、任务领取与模型工作流](docs/06-WORKBREAKDOWN-CODEX-WORKFLOW.md)
- [瑞成云接入](docs/07-SMS-PROVIDER-RUICHENG.md)
- [当前实施证据与剩余上线门](docs/08-IMPLEMENTATION-EVIDENCE.md)
- [可配置 Harness 运行时](docs/20-CONFIGURABLE-HARNESS-RUNTIME.md)
- [阿里云部署与上线手册](docs/22-ALIYUN-DEPLOYMENT-RUNBOOK.md)
- [Web 定价策略讨论记录](docs/23-PRICING-STRATEGY.md)
- [运营后台历史设计（已移除）](docs/24-OPERATIONS-ADMIN-PRD.md)
- [跨平台服务启停与运维脚本手册](docs/23-SERVICE-MANAGEMENT-SCRIPTS.md)
- [产品架构重构方案与分期开发计划](docs/24-PRODUCT-ARCHITECTURE-REFACTORING-AND-PLAN.md)
