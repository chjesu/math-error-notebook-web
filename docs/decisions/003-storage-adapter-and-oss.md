# ADR-003：统一存储边界与阿里云 OSS 适配

- 状态：已接受
- 日期：2026-08-30
- 范围：Phase 4 文件、图片、PDF 和导出对象

## 决策

应用继续以 `StorageAdapter` 为唯一文件存储边界。LocalFS 是本地实现；生产 OSS 使用阿里云 OSS Python SDK V2、V4 签名、私有对象、禁止覆盖和短时预签名 URL。数据库中的对象键保持供应商无关，不保存临时 URL。

生产装配必须显式选择 `STORAGE_PROVIDER=oss` 并提供 Region、Bucket、官方 HTTPS Endpoint 和凭据模式。任何缺失或不安全配置都在启动时失败，不自动回落 LocalFS。ECS 上优先使用实例 RAM Role 自动刷新 STS，本地联调才允许环境变量凭据。

用户图片在 `FileIntake` 信任边界内重新编码。系统先验证真实格式和像素上限，应用 EXIF Orientation，然后移除 EXIF、XMP、ICC、文本块和注释。规范化后的字节、大小和 SHA-256 才是存储及数据库记录的事实。

## 不变量与失败语义

- 对象键只能由应用生成并经过同一规范化校验；用户文件名永不作为 OSS 路径。
- 写入使用 `forbid_overwrite`。同键同哈希是幂等成功；同键不同哈希失败为碰撞。
- SDK、网络或凭据失败不得创建数据库文件记录；数据库写入失败只删除本次确实创建的对象。
- OSS 读取在返回内容前检查对象大小并再次限制累计字节数。
- 预签名 URL 只允许配置 Bucket 的阿里云 HTTPS 主机，最长 900 秒，且不得写日志。
- 删除保持幂等；用户注销仍先收敛领域状态，再删除该用户数据库列出的对象键。

## 威胁模型

- 欺骗：凭据由进程环境或 ECS RAM Role 提供，不接受请求参数中的身份。
- 篡改：对象禁止覆盖，上传携带内容摘要元数据，下载后仍由领域记录 SHA-256 复核。
- 信息泄露：Bucket/对象私有，短时签名按用户归属检查后生成，图片元数据被移除。
- 拒绝服务：上传字节、图片像素、OSS 读取大小和签名有效期均有上限。
- 权限提升：调用方只能使用服务端生成的对象键；Endpoint、Bucket、Region 和 URL 主机均校验。

## 依据

- [阿里云 OSS Python SDK V2 安装与客户端配置](https://www.alibabacloud.com/help/en/oss/developer-reference/2-0-manual-preview-version/)。
- [OSS SDK V2 预签名下载](https://www.alibabacloud.com/help/en/oss/developer-reference/download-an-object-using-a-signed-url-generated-with-oss-sdk-for-python-v2)与[预签名上传](https://www.alibabacloud.com/help/en/oss/developer-reference/upload-an-object-using-a-signed-url-generated-with-oss-sdk-for-python-v2)；V4 预签名最长七天，本项目进一步收紧到十五分钟。
- [OSS SDK V2 禁止同名覆盖](https://www.alibabacloud.com/help/en/oss/developer-reference/prevent-objects-from-being-overwritten-by-objects-that-have-the-same-names-2)与[对象元数据](https://www.alibabacloud.com/help/en/oss/developer-reference/manage-object-metadata-using-oss-sdk-for-python-v2)契约。
- 阿里云建议 ECS/ECI/ACK 使用实例 RAM Role 自动刷新 STS，避免硬编码长期 AccessKey。
- [Pillow 图像安全说明](https://pillow.readthedocs.io/en/stable/handbook/security.html)与[`exif_transpose` 文档](https://pillow.readthedocs.io/en/stable/reference/ImageOps.html)：保留解压炸弹保护，并在重新编码前应用方向信息。

## 非目标

- 本阶段不创建 Bucket、RAM Policy、WAF、KMS 或生产部署。
- 本阶段不改变认证、用户归属、数据库迁移或前端业务流程。
- 没有真实部署凭据时不宣称 OSS 联网验收完成。
