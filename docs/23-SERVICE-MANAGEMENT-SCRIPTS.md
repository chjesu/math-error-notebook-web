# 跨平台服务启停与运维脚本手册

> 文档基线：v0.4.0  
> 适用平台：Windows 10 / Windows 11 / Linux (Ubuntu, Debian, CentOS, RHEL, Arch, Alpine) / macOS / WSL

本文档规范说明李兆霖数学错题本 Web 项目的本地服务启动、后台守护、停止与跨平台迁移运维操作。

---

## 1. 脚本架构与全景清单

项目提供跨平台统一的启停命令入口，底层依托 Python（`scripts/local_env.py`）管理 MySQL 8 实例生命周期、Node.js Harness 运行时与 FastAPI/Uvicorn Web 进程。

```text
math-error-notebook-web/
├── start.bat                  # [Windows] 根目录一键启动（支持双击运行或命令行传参）
├── stop.bat                   # [Windows] 根目录一键停止（支持双击运行）
├── start.sh                   # [Linux/macOS] 根目录一键启动入口
├── stop.sh                    # [Linux/macOS] 根目录一键停止入口
├── .env                       # [本地私有] 环境变量与模型 API Key（已加入 .gitignore）
├── .env.example               # [配置模板] 环境变量与模型供应商示例
├── scripts/
│   ├── start.ps1              # [Windows] PowerShell 核心启动实现
│   ├── stop.ps1               # [Windows] PowerShell 核心停止实现
│   ├── start.sh               # [Linux] Bash 核心启动实现
│   ├── stop.sh                # [Linux] Bash 核心停止实现
│   └── local_env.py           # [跨平台核心] 统一环境探测、PID 管理与优雅关闭
└── data/runtime/              # [运行时目录]
    ├── service.pid            # 后台主服务 PID 记录
    ├── service.stdout.log     # 后台运行标准输出日志
    ├── service.stderr.log     # 后台运行错误日志
    └── deepseek-harness-web.* # Harness Web 工作台日志
```

---

## 2. Windows 10 / 11 操作指南（当前开发环境）

### 2.1 快速启动

- **方式 A（双击运行）**：
  直接在资源管理器中双击根目录的 `start.bat`。

- **方式 B（前台命令行启动）**：
  在终端中执行，可实时查看 Uvicorn 与 Harness 的访问日志：
  ```powershell
  .\start.bat
  # 或
  powershell -ExecutionPolicy Bypass -File scripts\start.ps1
  ```

- **方式 C（后台守护进程启动）**：
  不占用当前终端窗口，日志自动写入 `data/runtime/service.stdout.log`：
  ```powershell
  .\start.bat -Daemon
  # 或
  powershell -ExecutionPolicy Bypass -File scripts\start.ps1 -Daemon
  ```

### 2.2 快速停止

- **方式 A（双击停止）**：
  直接在资源管理器中双击根目录的 `stop.bat`。

- **方式 B（命令行停止）**：
  ```powershell
  .\stop.bat
  # 或
  powershell -ExecutionPolicy Bypass -File scripts\stop.ps1
  ```

停止脚本只把停止请求交给 `scripts/local_env.py stop`，由它作为唯一裁决者执行以下步骤：
1. 后台启动会先以独占创建方式领取 `data/runtime/service.pid` 所有权，再创建服务子进程；已有正常、失效或启动中记录都失败闭合，重复/并发启动不会覆盖旧服务身份。停止时读取其中的 PID、进程创建时间与可执行文件身份，三者与当前进程一致后才终止并验证后台服务进程树已经消失；身份不匹配时视为 PID 已复用，拒绝杀进程并保留记录；Harness 子进程及其随机回环端口随进程树一起回收；
2. 无论服务进程树停止是否成功，都继续尝试关闭本地私有 MySQL 实例（端口 3307）；
3. 只有确认服务进程树已停止才删除 PID 文件；若后台启动落正式身份失败且新进程树也无法回收，记录会转为含 PID、创建时间和可执行文件的 `recovery` 状态，后续 `stop` 仍可验证并精确回收。删除失败也返回非零退出码，任何失败均保留恢复状态，不按端口或宽泛进程名误杀其他程序。

---

## 3. Linux / macOS / WSL 操作指南（后续部署与迁移）

脚本采用 POSIX 兼容 Bash 编写，在 Linux（Ubuntu、Debian、CentOS、RHEL、Alpine、Arch）以及 macOS、WSL 环境下开箱即用。

### 3.1 权限设置（首次使用）

```bash
chmod +x start.sh stop.sh scripts/start.sh scripts/stop.sh
```

### 3.2 启动命令

- **前台启动（实时观察日志，Ctrl+C 退出）**：
  ```bash
  ./start.sh
  ```

- **后台守护进程启动**：
  ```bash
  ./start.sh --daemon
  ```

- **自定义端口启动**：
  ```bash
  ./start.sh --port 8080 --host 127.0.0.1
  ```

### 3.3 停止命令

```bash
# 标准安全停止
./stop.sh

# 强制清理模式（清理所有相关端口与匹配进程）
./stop.sh --force
```

---

## 4. 模型配置与环境变量管理

服务在启动时会自动读取项目根目录下的 `.env` 文件。

### 4.1 配置文件生成

从模板复制并编辑：
```bash
cp .env.example .env
```

### 4.2 常用模型通道配置

#### DeepSeek 官方多模态通道（默认推荐）
```env
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxx
HARNESS_MODEL=deepseek-v4-flash-vision-exp
HARNESS_BASE_URL=https://api.deepseek.com
HARNESS_INPUT_MODALITIES=text,image
```

#### 阿里百炼 / 通义千问（Qwen-VL）
```env
DASHSCOPE_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxx
HARNESS_API_KEY_ENV=DASHSCOPE_API_KEY
HARNESS_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
HARNESS_MODEL=qwen-vl-max
HARNESS_INPUT_MODALITIES=text,image
```

> **安全红线**：`.env` 包含 API 密钥，已配置在 `.gitignore` 中。严禁将带真实密钥的文件提交到 Git 仓库。

---

## 5. 端口矩阵与健康检查

| 端口 | 协议 | 关联服务 | 职责 |
| :--- | :--- | :--- | :--- |
| **`8000`** | HTTP/ASGI | 统一网关 / Uvicorn | 登录注册、业务 API、产品页面、Harness 静态资源、HTTP API 与 WebSocket |
| **`3307`** | TCP/MySQL | 本地隔离 MySQL 8 | 业务数据持久化，数据隔离于 `.runtime/local-mysql/` |

Harness Host 使用启动期随机回环端口，仅由 8000 网关访问，不构成用户可依赖的固定端口。

### 健康检查命令

在服务运行期间，可随时运行以下命令检查各组件健康状况：

```powershell
# 1. 运行系统全景诊断
python -X utf8 -B scripts/local_env.py doctor

# 2. 运行本地端到端链路 Smoke 测试
python -X utf8 -B scripts/local_env.py smoke

# 3. 运行全套单元测试
python -X utf8 -B -m unittest discover -s tests -p "test_*.py"
```

---

## 6. Linux 生产与 Systemd 扩展建议（后期落地）

当项目迁移到 Linux 生产服务器时，可直接通过 Systemd 服务单元托管 `start.sh`，示例配置文件 `/etc/systemd/system/math-notebook.service`：

```ini
[Unit]
Description=Lizhaolin Math Error Notebook Web Service
After=network.target

[Service]
Type=simple
User=appuser
WorkingDirectory=/opt/math-error-notebook-web
ExecStart=/opt/math-error-notebook-web/scripts/start.sh
ExecStop=/opt/math-error-notebook-web/scripts/stop.sh
Restart=always
RestartSec=5
EnvironmentFile=/opt/math-error-notebook-web/.env

[Install]
WantedBy=multi-user.target
```
