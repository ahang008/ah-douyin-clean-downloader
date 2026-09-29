---
name: ah-douyin-clean-downloader
description: 处理用户自有或已获授权的抖音内容。单条官方链接或口令默认下载无平台角标原视频；明确要求逐字稿时交付语义校对后的 Markdown 与 SRT；博主主页、名片或全部作品请求进入公开作品批量采集、原片保存和本地机器转写流程。
---

# AH 抖音下载与逐字稿

按用户意图选择模式，保留同一个 Skill 名称与安装入口。

| 输入或要求 | 执行与交付 |
| --- | --- |
| 单条视频官方链接或完整分享口令，未说明其他用途 | 默认下载原视频，无需追问。 |
| 单条视频明确要求“提取逐字稿、文案、转文字” | 本地识别后完成整篇语义校对，交付校对后的 Markdown 与原时间码 SRT，清理受控过程文件。先读 [single-transcript.md](references/single-transcript.md)。 |
| 博主官方主页、分享名片，或“全部作品、整号下载/转写” | 用专用正常 Edge 的官方网页 SDK 采集公开目录，再保留原片、原始机器 Markdown/SRT/识别证据、索引与合并合集。先读 [creator-batch.md](references/creator-batch.md)。 |

单条下载默认保存到 `~/Desktop/抖音无水印视频/<博主昵称>/`：

```bash
SKILL_DIR="${CODEX_HOME:-$HOME/.codex}/skills/ah-douyin-clean-downloader"
python3 "$SKILL_DIR/scripts/download_douyin.py" "<完整官方链接或分享口令>"
```

用户指定其他媒体库根目录时加 `--output-dir`；显式代理使用 `--proxy` 或已有 `AH_DOUYIN_PROXY`，不修改系统代理。成功回报本地绝对路径、博主目录、大小、时长和音视频校验结果；缺少 ffprobe 时说明仅完成文件头检查。

## 共同约束

- 只处理抖音官方输入与用户自有或已获授权的公开内容；单条视频不能证明整号完整。
- 使用平台播放源，不转码，不擦除作者已烧录的 Logo、字幕或贴片；播放源选择与维护细则见 [backend.md](references/backend.md)。
- 下载、单条转写和批量转写的保留规则各按本模式执行。单条校对成品清理过程文件；批量保留原片与原始机器稿，不能套用单条清理规则。
- 批量脚本不调用托管大模型、付费 ASR 或模型 Computer Use。机器稿的技术完整性不等于语义准确；全批语义校对须另按用户要求执行，使用额外模型用量，并另存校对版。
- 专用 Edge 资料目录会按浏览器正常行为保存本次登录，是私有运行数据。不要读取或复制日常浏览器会话，不打包资料目录、登录截图、会话信息或私人内容。
- 保留已验证成果；失败报告实际阶段与缺失项，不把首页计数、窗口出现或样本成功称为全量完成。

环境自检可用 `python3 scripts/doctor.py --format text`。下载只需 Python 3.9+ 与 curl；MLX 转写和整号流程使用 Apple Silicon Mac、macOS 15+、Python 3.12，按对应参考文件准备依赖。故障时读 [troubleshooting.md](references/troubleshooting.md)。
