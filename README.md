# ah-douyin-clean-downloader

把抖音分享口令或官方链接发给 Codex，可以下载平台提供的无抖音角标原视频，也可以只得到一份校对后的 Markdown 逐字稿。

这是 [阿杭 Skills](https://github.com/ahang008/ah-skills) 旗下的独立 Skill 仓库。

该工具不使用付费解析或语音识别 API，不调用视觉模型，不保存 Cookie、账号或密钥。逐字稿使用本地 Whisper 识别，过程视频和机器稿在成品验收后清理。

![工作流](assets/workflow.png)

## 直接交给 Codex 安装

把下面这句话原样发给 Codex：

```text
安装这个 Skill：https://github.com/ahang008/ah-douyin-clean-downloader
```

这个仓库的根目录就是 Skill 目录，包含 `SKILL.md`、脚本、测试和完整说明。Codex 可以使用内置的 `skill-installer` 安装单个 Skill，不需要下载整个阿杭 Skills 集合。

对应的标准安装命令是：

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-installer/scripts/install-skill-from-github.py" \
  --repo ahang008/ah-douyin-clean-downloader \
  --path . \
  --name ah-douyin-clean-downloader
```

安装完成后，新开一轮 Codex 对话即可使用。

## 使用边界

- 仅用于你自有或已获授权的视频。
- 只获取平台已提供的播放源，不对画面做 AI 修复。
- 可去除抖音分发阶段的平台角标，不能去除作者上传前已烧录的 Logo、字幕或贴片。
- 使用者需自行遵守平台规则、著作权规则和所在地法律。

## 环境要求

- Python 3.9 或更高版本。
- `curl`，用于访问分享页、元数据和视频源。
- `ffprobe` 可选，安装后可验证时长、编码和音视频流。
- 逐字稿模式额外需要 `ffmpeg`、`mlx_whisper` 和本地 Whisper 模型。
- macOS、Linux 或带 Python 和 curl 的 Windows 环境。

## 安装为 Codex Skill

如果不使用 `skill-installer`，也可以直接克隆到 Codex Skills 目录：

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
git clone --depth 1 https://github.com/ahang008/ah-douyin-clean-downloader.git \
  "${CODEX_HOME:-$HOME/.codex}/skills/ah-douyin-clean-downloader"
```

最终安装位置为：

```text
~/.codex/skills/ah-douyin-clean-downloader/
```

先执行环境自检：

```bash
SKILL_DIR="${CODEX_HOME:-$HOME/.codex}/skills/ah-douyin-clean-downloader"
python3 "$SKILL_DIR/scripts/doctor.py" --format text
```

安装完成后，直接把抖音分享口令发给 Codex 即可。纯链接或纯口令默认会执行下载；如果当次要分析或总结，需要在消息中明确说明。

如果只要逐字稿，在口令后说明“提取逐字稿”。最终目录只保留一个校对后的 `.md` 文件，不保留视频、音频、字幕或识别结果。

## 默认分类

```text
~/Desktop/抖音无水印视频/
└── <博主昵称>/
    └── 抖音-<视频标题>-<作品ID>.mp4
```

第一次遇到某个博主时自动建立子文件夹，以后同一博主的视频自动放入该文件夹。博主昵称缺失时放入 `未知博主`。

## 命令行使用

```bash
python3 scripts/download_douyin.py "<完整抖音分享口令或链接>"
```

指定媒体库根目录：

```bash
python3 scripts/download_douyin.py "<分享口令>" --output-dir "/absolute/path"
```

如果当前网络需要代理：

```bash
export AH_DOUYIN_PROXY="http://127.0.0.1:7890"
python3 scripts/download_douyin.py "<分享口令>"
```

脚本成功时输出 JSON，包含媒体库路径、博主文件夹、视频绝对路径、大小、时长、编码和音视频流数量。

只生成逐字稿：

```bash
python3 scripts/transcribe_douyin.py "<完整抖音分享口令或链接>"
```

该命令生成临时机器识别稿，供 Codex 完成语义校对。校对成品写入脚本返回的 `final_path` 后，必须清理 `work_dir`，因此用户最终只会得到校对后的 Markdown 逐字稿。

## 测试

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/*.py tests/*.py
```

测试使用合成元数据，不保存真实用户视频或私密链接。

## 项目来源

这是为 Codex 桌面工作流编写的轻量实现。项目来源和外部参考见 [PROVENANCE.md](PROVENANCE.md) 和 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 许可证

本项目为源码可见软件，允许个人学习、研究和非商业使用。未经书面授权不得用于商业用途。

该许可证不属于 OSI 认可的开源许可证。完整条款见 [LICENSE](LICENSE)。

## 阿杭 Skills

其他公开 Skill 和后续更新入口见 [ahang008/ah-skills](https://github.com/ahang008/ah-skills)。每个 Skill 都使用独立仓库，方便把单个链接直接交给 Codex 安装。
