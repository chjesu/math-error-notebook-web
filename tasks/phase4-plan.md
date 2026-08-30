# Phase 4：存储适配器与阿里云 OSS

## 目标

在不改变 `web_files.object_key`、用户归属和现有下载 API 的前提下，把 LocalFS 与阿里云 OSS 收敛到同一个存储契约。生产选择 OSS 时失败关闭，不允许自动回落到本地磁盘。

## 冻结契约

- 数据库只保存应用生成的对象键、内容哈希、媒体类型和大小；供应商 URL 不是事实源。
- `StorageAdapter` 负责幂等写入、受限读取、幂等删除和短时预签名请求；LocalFS 不伪造公网 URL。
- OSS 对象固定为私有、禁止覆盖；同键同哈希返回“已存在”，同键不同内容判定冲突。
- OSS Endpoint 只接受与配置 Region 一致的阿里云 HTTPS 公网或内网域名。
- 凭据仅来自环境变量或 ECS RAM Role；源码、日志、数据库和签名返回结构不保存 AccessKey/STS。
- 预签名 URL 是短期 Bearer 凭证，有效期最多 900 秒；签名头必须原样返回给调用方。
- PNG/JPEG 在存储前解码、应用 EXIF Orientation、限制像素并重新编码；EXIF/XMP/ICC/注释不进入存储对象。
- PDF 继续由领域层生成字节，由 `NotebookService` 通过注入的 `StorageAdapter` 保存和读取。

## 增量

1. 扩展稳定存储契约和 LocalFS 能力声明。
2. 实现 OSS SDK V2 适配器、配置工厂、短时上传/下载签名。
3. 在 `FileIntake` 边界加入图片规范化。
4. 验证图片、PDF、导出和删除均只依赖存储接口。
5. 更新配置、架构、阶段状态和可重复 OSS smoke 说明。

## 验证

- 聚焦运行存储、文件、PDF、领域及 ASGI 上传下载测试。
- 对全部变更 Python 文件做 AST 语法解析。
- 检查补丁空白、对象键穿越、签名 URL 主机、凭据字面量和无关变更。
- 不运行全量测试，遵循用户本阶段指示。

## 外部验收

真实 Bucket 联网 smoke 需要部署端提供 RAM Role 或进程环境凭据；没有凭据时只完成确定性 fake-client 契约验证，不伪报真实 OSS 已通过。
