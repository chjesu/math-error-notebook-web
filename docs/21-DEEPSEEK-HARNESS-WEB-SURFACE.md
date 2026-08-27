# DeepSeek Harness 原生工作台前端

## 决策

工作台不再复刻 DeepSeek Harness 的视觉样式，而是直接运行其 MIT 许可的官方 Web Host 与 `@deepseek-ai/dsh-web-frontend@0.1.1-rc.2`。该版本与项目已固定的 Harness 核心版本一致。会话树、历史分页、附件输入、流式消息、上下文计量、自动压缩、模型选择、设置和原生交互均来自官方包；项目只增加品牌、错题本入口和学生产品边界。

这意味着后续升级由依赖版本和组合补丁完成，不再维护一套看起来相似、能力却逐步分叉的聊天界面。

## 本地拓扑

```text
浏览器 http://127.0.0.1:8000/
  ├─ GET /v1/session：复用产品登录态，未登录跳转 /login
  └─ iframe http://127.0.0.1:3080/
       └─ 官方 dsh --profile web Host + 官方 Web 前端
```

`scripts/local_env.py serve --enable-harness-ui` 负责启动和停止 3080 端口的官方 Host。其 `DSH_HOME` 固定为 Git 忽略的 `data/runtime/deepseek-harness-web-home`，避免读取用户电脑上其他 Harness 项目的会话。标准本地启动命令为：

```powershell
python -X utf8 -B scripts/local_env.py serve --host 127.0.0.1 --port 8000 --enable-harness-model --enable-harness-ui
```

## 产品定制与安全边界

- `extensions/dsh-math-notebook-ui/` 注册李兆霖数学错题本 Logo、名称、“错题本与复习”入口和固定工作区边界，不复制官方 React 组件。
- 启动时自动注册 Git 忽略目录中的固定“错题会话”工作区；学生界面隐藏开发者使用的工作区选择和添加入口，会话与历史能力保持不变。
- 启动器把项目自带的 `math-notebook` 预设同步到 Git 忽略的 Harness 运行目录；会话只保留数学助手身份，不挂载开发者 Shell、文件编辑、技能或目标工具。
- `config/deepseek-harness/web-product.patch.yml` 注入数学错题助手提示，并禁用 Shell、文件系统写入、子智能体、工作流、目标和编程编辑器等学生产品不需要的能力。
- Harness Web 运行在只读 sandbox、`approval=never`、关闭遥测的本地配置下。
- 账号注册、登录、短信、限流、用户数据归属、题目版本冻结和正式入库仍由 Python/MySQL 确定性代码负责，模型与前端不能绕过这些边界。
- 当前 8000/3080 双端口嵌入只用于 localhost 测试。生产必须通过同源网关统一认证、WebSocket 和安全响应头，不能直接暴露 3080。

## 保留的产品页面

错题本、今日复习、练习 PDF、学习进度、设置与隐私继续使用现有独立 URL。官方 Harness 侧栏底部提供“错题本与复习”入口；退出账号仍只出现在产品的设置与隐私页面。

## 官方来源

- DeepSeek Harness：<https://github.com/deepseek-ai/deepseek-harness>
- 官方 Web 应用清单：<https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/web/package.json>
- 官方客户端说明：<https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/client/README.md>
- 会话 UI：<https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/client/ui-conversation/README.md>
- 侧栏 UI：<https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/client/ui-sidebar/README.md>
