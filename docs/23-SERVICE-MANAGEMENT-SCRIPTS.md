# 本地 C/S 模拟：服务启停与验证

本模式模拟阿里云 ECS 的客户端/服务端边界，但只绑定本机。它不是生产部署入口，短信和验证码仍为本地模拟。

## 唯一浏览器入口

浏览器访问 `http://127.0.0.1:8000/`。登录、会话、错题本、练习 PDF、学习进度、下载和 WebSocket 都经过同一入口。前端使用当前页面的 origin，不写死另一个主机或端口。

Harness 使用启动时随机分配的回环端口；MySQL 8 使用 `127.0.0.1:3307`。这两个内部服务不作为浏览器入口，不需要开放安全组。真实 ECS 还需要 HTTPS、反向代理、生产认证、持久存储、备份及独立安全验收。

## 启停

Windows：

```powershell
.\start.bat -Daemon
.\stop.bat
```

不加 `-Daemon` 时前台运行。可用 `-Port 8123` 更换对外本地端口，再访问相应端口；Host 仅允许 `127.0.0.1` 或 `localhost`。

Linux / macOS / WSL 的本地入口（仍需安装 MySQL 8、Node.js 和 Python 依赖；本次仅实测 Windows）：

```bash
./start.sh --daemon
./stop.sh
```

启动脚本优先使用项目 `.venv`。启动前校验插件 JavaScript 和配置合并标记，后台模式等待健康检查通过后才报告成功。端口占用或 PID 记录已存在时不会覆盖现有服务。

停止时按记录的 PID、创建时间和可执行文件核对身份，只回收本项目的进程树，并停止本地私有 MySQL；不清空数据，不按端口或进程名称批量终止。失败时保留恢复信息，不提供绕过身份核对的强制清理模式。

## 模型与数据

沿用当前 Qwen 配置：`HARNESS_MODEL=qwen3.8-flash`，`HARNESS_API_KEY_ENV=DASHSCOPE_API_KEY`。密钥放操作系统用户变量，或仅放 Git 忽略的 `.env`；进程环境优先。不要把真实密钥发到会话、写进文档或提交 Git。更新用户变量后，需让启动服务的终端继承新环境。

数据库、上传图片、PDF、Harness 历史仍保存在原有忽略目录，不随服务重启清空：
- `.runtime/local-mysql/`：MySQL 与业务文件；
- `data/runtime/deepseek-harness-web-home/`：工作台会话；
- `data/runtime/service.pid`：带身份校验的进程记录；
- `data/runtime/service.*.log`：启动与运行日志。

当前会话继续使用“先冻结图片转录、再提交判题”的既有工具链，保留复习码关联、题库冲突复核、订正与延期学习。合并保留的后台批次 API 尚未替换这条默认会话链路。

## 检查与排错

```powershell
.\.venv\Scripts\python.exe -X utf8 -B scripts/local_env.py doctor
.\.venv\Scripts\python.exe -X utf8 -B scripts/local_env.py smoke
.\.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -p "test_*.py"
```

`smoke` 使用临时测试账号并定向清理，不清除已有学习数据。所有测试通过只表示本地候选可用，不代表数学准确率评测或生产安全签署。

打开页面失败时，先检查 `/healthz` 和启动日志；插件注册失败时检查源文件合并状态及语法，不通过重传学生照片解决。不要删除数据库、会话目录或 PID 文件来掩盖错误；先核对记录对应的进程。
