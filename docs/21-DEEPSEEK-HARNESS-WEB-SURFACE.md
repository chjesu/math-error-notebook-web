# DeepSeek Harness 原生工作台前端

## 决策

工作台不再复刻 DeepSeek Harness 的视觉样式，而是直接运行其 MIT 许可的官方 Web Host 与 `@deepseek-ai/dsh-web-frontend@0.1.1-rc.2`。该版本与项目已固定的 Harness 核心版本一致。会话树、历史分页、附件输入、流式消息、上下文计量、自动压缩、模型选择、设置和原生交互均来自官方包；项目只增加品牌、错题本入口和学生产品边界。

这意味着后续升级由依赖版本和组合补丁完成，不再维护一套看起来相似、能力却逐步分叉的聊天界面。

## 本地拓扑

```text
浏览器 http://127.0.0.1:8000/
  ├─ /login、/register、协议页及其固定样式/脚本：公开认证外壳
  ├─ /assets/branding/*：公开品牌资源
  ├─ /manifest.webmanifest：产品层公开静态清单
  └─ 已登录的 /、/assets/*、/plugins/*、/api/*、WebSocket
       └─ 8000 同源网关 → 随机回环端口上的官方 dsh --profile web Host
```

`scripts/local_env.py serve --enable-harness-ui` 负责启动和停止官方 Host，并在启动时分配不向浏览器公开的回环端口。端口只从子进程私有 stdout 读取；stdout 与 stderr 都先经过私有管道，将回环 URL 中的端口掩码后才写日志。Windows 使用非零 `CREATE_SUSPENDED` 创建标志，在绑定 Job Object 后验证线程确实从挂起状态恢复；Unix 使用独立进程组。其 `DSH_HOME` 固定为 Git 忽略的 `data/runtime/deepseek-harness-web-home`，避免读取用户电脑上其他 Harness 项目的会话。标准本地启动命令为：

```powershell
python -X utf8 -B scripts/local_env.py serve --host 127.0.0.1 --port 8000 --enable-harness-model --enable-harness-ui
```

## 产品定制与安全边界

- `extensions/dsh-math-notebook-ui/` 注册李兆霖数学错题本 Logo、名称、错题本/练习 PDF/学习进度导航、官方设置内的“账号与隐私”分区和固定工作区边界；工作台与设置均不重复显示入口。
- 数学助手固定采用六段式结果：题目整理、学生作答还原、错因分析与点评、知识点梳理、详细解析、最终答案；小建议放在最终答案正文下方，以 `*（小建议：……）*` 斜体显示，不单独成段。系统回执另行显示错题本记录状态。错因和知识点属于正式错题诊断，不能只存在于聊天文本。
- 每轮图片判题及必要复核结束后，权威工具回执根据真实状态给出下一步；助手最终回复以“下一步”收尾，只提示一个当前最优先、可立即执行的动作。
- 启动时自动注册 Git 忽略目录中的固定“错题会话”工作区；学生界面隐藏开发者使用的工作区选择和添加入口，会话与历史能力保持不变。
- 启动器把项目自带的 `math-notebook` 预设同步到 Git 忽略的 Harness 运行目录；会话只保留数学助手身份，不挂载开发者 Shell、文件编辑、技能或目标工具。
- `config/deepseek-harness/web-product.patch.yml` 注入数学错题助手提示，并禁用 Shell、文件系统写入、子智能体、工作流、目标和编程编辑器等学生产品不需要的能力。
- Harness Web 运行在只读 sandbox、`approval=never`、关闭遥测的本地配置下。
- 账号注册、登录、短信、限流、用户数据归属、题目版本冻结和正式入库仍由 Python/MySQL 确定性代码负责，模型与前端不能绕过这些边界。
- 浏览器只访问 8000 同源网关；Harness 页面、资源、HTTP API 和 WebSocket 在服务端会话校验后转发。内部随机端口只接受回环连接，不是用户入口，也不进入启动输出。
- 网关对请求与响应头使用正向白名单，不把产品 Cookie、授权头、身份头或上游 `Set-Cookie` 带过边界；允许返回的 `Location`、`Content-Location`、`Link` 与 CSP 中如含上游权威地址，必须改写为 8000 公共同源地址，不能泄露内部端口。WebSocket 上游在升级后异常消失统一返回可重试的 `1013`，浏览器断开时取消并收集全部桥接任务。Uvicorn 必须安装 `requirements.txt` 声明的 `websockets` 传输实现。浏览器默认不带会话 Cookie 请求 PWA manifest，因此该无敏感信息清单仅接受 `GET`，由产品层直接公开提供，不反向代理 Harness。
- 本地 Harness 会在浏览器中把当前不透明会话编号绑定到 8000 端口已经认证的个人账号。最新用户消息含图片时，模型必须完整识别本轮全部图片中的全部题目，并恰好调用一次 `process_error_notebook_attachments`；Host 读取实际附件字节，按图片逐次调用仅限回环地址和启动期随机令牌保护的内部接口。Python 服务复用现有文件隔离、题目冻结、判题候选、题库交叉验证、错因/知识点持久化、错题提交和复习排程，不再发起第二次模型调用。工具结果是权威业务结果和入本回执，并直接成为原 Harness 会话事件，因此刷新、翻页和重新打开会话后仍可查看。`confirm_error_notebook_entry` 只保留给其他已经生成冻结候选的流程；令牌不写入仓库、日志或浏览器。
- 题库答案不会在独立解题前交给模型。只有冻结答案未通过确定性一致性校验时，服务端才在 `reference_review` 中返回已验证当前版本的答案与解析；会话必须恰好调用一次 `adjudicate_error_notebook_reference_conflicts`，批量提交“一致、确有冲突、无法确认”及具体依据。该接口重新校验登录用户、Harness 会话、候选、输入版本与两份答案哈希：复核一致时沿用独立判题；确认实质冲突时必须提交基于题库解析的重判结果，服务端强制以题库答案和解析替换最终答案后继续业务流程；无法确认、版本变化或授权失效时保持未入本。

桥接接口按当前用户、操作版本和有序附件哈希保持幂等：相同批次重试返回同一批次，不重复生成文件、题目、错题或复习任务。正确题明确回执“未计入”，错误或部分正确且诊断完整时自动入本，证据不足或题库解析冲突时保留候选并等待复核。模型不能自行生成成功回执，也不能把内部 `candidate_id`、`input_version` 或 batch id 暴露给学生。

## 保留的产品页面

错题本、练习 PDF、学习进度、设置与隐私继续使用独立 URL。今日复习计划保留在错题本内；六阶段规则、记忆原理与按月展示每日错题及当前阶段的错题日历归入学习进度。官方 Harness 侧栏保留三项学习导航；账号与隐私入口合并到已有“设置”面板，退出账号仍只出现在产品的设置与隐私页面。

## 官方来源

- DeepSeek Harness：<https://github.com/deepseek-ai/deepseek-harness>
- 官方 Web 应用清单：<https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/web/package.json>
- 官方客户端说明：<https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/client/README.md>
- 会话 UI：<https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/client/ui-conversation/README.md>
- 侧栏 UI：<https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/client/ui-sidebar/README.md>
