# 可配置 Harness 运行时

## 当前结论

工作台使用固定版本 `0.1.1-rc.2` 的 DeepSeek Harness 核心组件承载持续会话、JSONL 持久化、事件、上下文计量、自动压缩和有界重试。DeepSeek 只是当前默认模型通道，错题业务不依赖供应商名称。

统一入口是 `services/web_app/harness_runtime.py`，组合配置是 `config/deepseek-harness/cordis.yml`。`NotebookAsgiApp` 仍只调用既有的 `extract`、`grade`、`chat_turn`、`history` 和 `compact`，认证、用户归属、版本冻结、判题质量门和正式写库均由确定性 Python 代码负责。

## 启动

先安装锁定依赖，再启动本地服务：

```powershell
npm ci --ignore-scripts --no-audit --no-fund
python -X utf8 -B scripts/local_env.py serve --host 127.0.0.1 --port 8000 --enable-harness-model
```

默认通道读取 `DEEPSEEK_API_KEY`，默认模型为当前账号可用的 `deepseek-v4-flash-vision-exp`。DeepSeek 官方于 2026-08-21 将其作为实验性多模态模型发布；官方 Vision 指南确认它支持 JPEG、PNG、GIF 和 WebP，并通过 OpenAI 兼容 Chat Completions 的 `content` 数组与 `image_url` 接收 Base64 或 URL 图片。图片只能出现在 `user` 消息中，其他 DeepSeek 模型收到图片会返回 400。密钥只能通过环境或密钥服务提供，禁止写入仓库、日志或文档。

当前本地链路已分别完成一次真实文本会话和一次真实图片请求。图片请求先由 Harness 官方附件模块生成持久引用，再由 pi-ai 转换为供应商请求；未使用文件路径或自建视觉转写协议。

## 供应商配置

| 变量 | 用途 | 默认值 |
|---|---|---|
| `HARNESS_PROVIDER` | Harness 路由 ID | `notebook-provider` |
| `HARNESS_PROVIDER_NAME` | 供应商显示名 | `Notebook model provider` |
| `HARNESS_API_KEY_ENV` | 保存 API Key 的环境变量名称 | `DEEPSEEK_API_KEY` |
| `HARNESS_API_PROTOCOL` | pi-ai 协议 | `openai-completions` |
| `HARNESS_BASE_URL` | OpenAI 兼容网关根地址 | `https://api.deepseek.com` |
| `HARNESS_MODEL` | 模型 ID | `deepseek-v4-flash-vision-exp` |
| `HARNESS_INPUT_MODALITIES` | 模型输入能力 | `text,image` |
| `HARNESS_REASONING` | 可选推理强度 | 未设置，沿用供应商默认 |
| `HARNESS_MAX_TOKENS` | 单轮输出上限 | `32768` |

不要给不支持推理强度的自定义模型设置 `HARNESS_REASONING`，也不要把仅文本模型声明为 `text,image`。能力声明与网关实际能力不一致时，供应商会拒绝请求，服务端不会写入候选或错题。

## 切换到 Qwen

Qwen 使用 OpenAI 兼容通道时只需替换运行环境，例如：

```powershell
$env:HARNESS_PROVIDER = "qwen-compatible"
$env:HARNESS_PROVIDER_NAME = "Qwen"
$env:HARNESS_API_KEY_ENV = "DASHSCOPE_API_KEY"
$env:HARNESS_API_PROTOCOL = "openai-completions"
$env:HARNESS_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
$env:HARNESS_MODEL = "<经离线评测的多模态模型 ID>"
$env:HARNESS_INPUT_MODALITIES = "text,image"
python -X utf8 -B scripts/local_env.py serve --host 127.0.0.1 --port 8000 --enable-harness-model
```

模型 ID 和能力以切换当日的供应商文档、账号模型列表和离线题目集验证为准。无需修改前端、API、Schema 或错题本流程。

## 数据与故障边界

- 图片先由官方本地附件模块校验、去元数据、归一化并保存为内容寻址对象，再向模型传递持久引用。
- Harness 会话和产品历史投影保存在 Git 忽略的 `data/runtime/`；页面刷新和 Web 服务重启后可以分页恢复。
- 审计只记录任务、供应商、模型、耗时、公开错误分类和供应商错误码；不记录密钥、图片、题目正文或提示词。
- Harness JSON-RPC 固定版没有单会话关闭、单提示取消和强制手动压缩协议；当前启用自动压缩，停止回合仍使用现有应用级中断边界。这些限制不能标记为已完成的上游能力。

## 官方参考

- DeepSeek Vision 指南：<https://api-docs.deepseek.com/guides/vision/>
- DeepSeek 2026-08-21 更新记录：<https://api-docs.deepseek.com/updates/>
- DeepSeek Chat Completions API：<https://api-docs.deepseek.com/api/create-chat-completion/>
