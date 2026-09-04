# 可配置 Harness 运行时

## 当前结论

工作台使用固定版本 `0.1.1-rc.2` 的 DeepSeek Harness 核心组件承载持续会话、JSONL 持久化、事件、上下文计量、工具结果裁剪、自动/手动压缩、中断和有界重试。当前默认模型通道是阿里云百炼 `qwen3.8-flash`，错题业务不依赖供应商名称。

统一入口是 `services/web_app/harness_runtime.py`，组合配置是 `config/deepseek-harness/cordis.yml`。`NotebookAsgiApp` 仍只调用既有的 `extract`、`grade`、`chat_turn`、`history` 和 `compact`，认证、用户归属、版本冻结、判题质量门和正式写库均由确定性 Python 代码负责。

## 启动

先安装锁定依赖，再启动本地服务：

```powershell
npm ci --ignore-scripts --no-audit --no-fund
python -X utf8 -B scripts/local_env.py serve --host 127.0.0.1 --port 8000 --enable-harness-model --enable-harness-ui
```

默认通道读取 `DASHSCOPE_API_KEY`，默认模型为 `qwen3.8-flash`。百炼官方模型资料声明该模型支持视觉、Function Calling、100 万 Token 上下文和最多 32768 Token 输出。项目把这两个容量显式交给 Harness，避免 pi-ai 使用通用默认值错误判断上下文溢出。密钥只能通过环境或密钥服务提供，禁止写入仓库、日志或文档。

当前本地链路已分别完成一次真实文本会话和一次真实图片请求。图片请求先由 Harness 官方附件模块生成持久引用，再由 pi-ai 转换为供应商请求；未使用文件路径或自建视觉转写协议。

Qwen 通道通过 pi-ai 的原生 `cacheRetention: short` 和 `cacheControlFormat: anthropic` 发送百炼支持的显式 `cache_control`。固定的系统提示词、工具定义和持续会话前缀可复用；题目、图片和工具结果继续作为动态后缀。没有启用 OpenAI 专用的 24 小时缓存参数。实际命中量以 Harness 上报的 `cacheReadTokens` 为准，供应商不保证每次都命中。

## 供应商配置

| 变量 | 用途 | 默认值 |
|---|---|---|
| `HARNESS_PROVIDER` | Harness 路由 ID | `notebook-provider` |
| `HARNESS_PROVIDER_NAME` | 供应商显示名 | `Qwen3.8 Flash` |
| `HARNESS_API_KEY_ENV` | 保存 API Key 的环境变量名称 | `DASHSCOPE_API_KEY` |
| `HARNESS_API_PROTOCOL` | pi-ai 协议 | `openai-completions` |
| `HARNESS_BASE_URL` | OpenAI 兼容网关根地址 | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `HARNESS_MODEL` | 模型 ID | `qwen3.8-flash` |
| `HARNESS_INPUT_MODALITIES` | 模型输入能力 | `text,image` |
| `HARNESS_REASONING` | 可选推理强度 | 未设置，沿用供应商默认 |
| `HARNESS_CONTEXT_WINDOW` | 模型上下文总容量 | `1000000` |
| `HARNESS_MAX_TOKENS` | 单轮输出上限 | `32768` |

不要给不支持推理强度的自定义模型设置 `HARNESS_REASONING`，也不要把仅文本模型声明为 `text,image`。能力声明与网关实际能力不一致时，供应商会拒绝请求，服务端不会写入候选或错题。

## 切换到 Qwen

Qwen 使用 OpenAI 兼容通道时只需替换运行环境，例如：

```powershell
$env:HARNESS_PROVIDER = "notebook-provider"
$env:HARNESS_PROVIDER_NAME = "Qwen3.8 Flash"
$env:HARNESS_API_KEY_ENV = "DASHSCOPE_API_KEY"
$env:HARNESS_API_PROTOCOL = "openai-completions"
$env:HARNESS_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
$env:HARNESS_MODEL = "qwen3.8-flash"
$env:HARNESS_INPUT_MODALITIES = "text,image"
$env:HARNESS_CONTEXT_WINDOW = "1000000"
$env:HARNESS_MAX_TOKENS = "32768"
python -X utf8 -B scripts/local_env.py serve --host 127.0.0.1 --port 8000 --enable-harness-model --enable-harness-ui
```

模型 ID 和能力以切换当日的供应商文档、账号模型列表和离线题目集验证为准。无需修改前端、API、Schema 或错题本流程。

## 数据与故障边界

- 图片先由官方本地附件模块校验、去元数据、归一化并保存为内容寻址对象，再向模型传递持久引用。
- Harness 会话和产品历史投影保存在 Git 忽略的 `data/runtime/`；页面刷新和 Web 服务重启后可以分页恢复。
- 数学预设加载 Harness 官方压缩组：8 KiB 以上工具结果先裁剪，达到上下文压力阈值时自动压缩；`/compact` 和产品整理入口执行真实、持久化的会话压缩。
- 停止单轮通过 Harness `session/cancel` 中断当前 Agent，不再终止整个共享运行时。
- 审计只记录任务、供应商、模型、耗时、公开错误分类和供应商错误码；不记录密钥、图片、题目正文或提示词。
- Shell、文件写入、技能、子智能体和开发工作流仍按学生产品安全边界关闭；“完整接入”指完整使用与错题会话有关的 Harness 能力，不扩大模型的数据与系统权限。

## 官方参考

- 阿里云百炼 qwen-flash 模型信息：<https://help.aliyun.com/zh/model-studio/qwen-flash>
- 阿里云百炼视觉理解模型：<https://help.aliyun.com/zh/model-studio/vision-model>
