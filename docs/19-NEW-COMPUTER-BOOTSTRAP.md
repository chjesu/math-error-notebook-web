# 新电脑本地初始化

本文只适用于 Windows 上的 localhost 本地模拟环境，不是生产部署说明。

## 1. Git 之外的前置资源

- Python 3，并能通过 `python` 命令调用；
- MySQL Server 8.4，默认安装在 `C:\Program Files\MySQL\MySQL Server 8.4`；
- 能安装 `requirements.txt` 中 Python 包的网络或离线包源；
- 可选：Codex CLI 及已完成的本机登录，仅在启用图片识别、判题和连续对话时需要。

MySQL 安装在其他目录时，只在当前终端设置：

```powershell
$env:LZLM_LOCAL_MYSQL_HOME = "D:\Apps\MySQL\MySQL Server 8.4"
```

## 2. 从空白数据直接启动

克隆仓库后，在仓库根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\bootstrap_local.ps1
```

脚本会依次创建 `.venv`、安装依赖、创建本机随机密钥、初始化
`127.0.0.1:3307/lzlm_web_local`、执行全部数据库迁移、运行 smoke，随后在
`http://127.0.0.1:8000` 启动服务。

只初始化和验收，不保持 Web 服务运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\bootstrap_local.ps1 -NoServe
```

启用 Codex 模型功能：

```powershell
codex --version
powershell -ExecutionPolicy Bypass -File scripts\bootstrap_local.ps1 -EnableCodexModel
```

## 3. 初始化数据资源

空白初始化只包含 Git 中已版本化的数据库迁移，生成以下本地资源：

| 资源 | 位置 | 初始内容 |
|---|---|---|
| MySQL 数据目录 | `.runtime/local-mysql/data/` | 空账号、空错题本、空题库及完整 Schema |
| 本机随机密钥 | `.runtime/local-mysql/secrets.json` | 初始化时随机生成，不得提交或跨环境共用 |
| 上传隔离区 | `.runtime/local-mysql/quarantine/` | 空目录，后续保存用户上传文件 |
| 模型候选 | `.runtime/local-mysql/model-candidates/` | 空目录，可重建 |
| 迁移账本 | MySQL + `.runtime/local-mysql/ready` | 记录已执行迁移及 SHA-256 |

仓库不附带现有用户、手机号、密码、会话、上传图片、错题记录或正式数学题库。
空题库不会阻止注册、上传、判题和错题整理，但推荐页会提示缺少合格推荐题。

如有已获授权的桌面权威题库，可在初始化完成后先 dry-run，再显式导入：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B scripts\migrate_question_bank.py --source-root <桌面错题本目录>
.\.venv\Scripts\python.exe -X utf8 -B scripts\migrate_question_bank.py --source-root <桌面错题本目录> --commit --rights-confirmed
```

不得把 `math_notebook.db`、题目图片或用户上传文件加入本仓库。

## 4. 验收与停止

```powershell
.\.venv\Scripts\python.exe -X utf8 -B scripts\local_env.py status
.\.venv\Scripts\python.exe -X utf8 -B scripts\local_env.py smoke
.\.venv\Scripts\python.exe -X utf8 -B scripts\local_env.py stop
```

`status` 的检查项应全部为 `true`。服务固定监听 localhost，不能通过修改参数直接用于局域网或公网。

## 5. 重置原则

需要重新初始化时，先备份所需业务数据。`.runtime/local-mysql/` 同时包含数据库、上传文件和密钥，删除它会丢失全部本地状态。不要从正在运行的 MySQL 数据目录直接复制文件；保留数据迁移应使用一致性数据库备份，并单独迁移上传隔离区。
