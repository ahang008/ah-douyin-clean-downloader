---
name: ah-douyin-clean-downloader
description: 解析用户提供的抖音分享口令或官方链接。用户只发送链接或口令时，下载平台播放源中的无抖音水印原视频；用户要求提取逐字稿、文案或转文字时，在本地识别并交付校对后的 Markdown 逐字稿与逐段时间码 SRT。仅用于用户自有或已获授权的内容。
---

# AH 抖音下载与逐字稿

根据用户意图选择下载或逐字稿模式。两种模式都不调用付费 API。

## 默认触发

- 用户只发送一条抖音官方链接或完整分享口令时，直接下载，不再追问是否下载。
- 用户明确要求“提取逐字稿、提取文案、转文字”时，执行逐字稿模式。
- 用户要求分析或总结时，先取得文字，再按当次要求处理；最终只交付用户要求的成品。
- 用户明确说不要下载或只查看时，不保留视频文件。

## 首次使用

如果用户问能否运行、安装状态或环境是否齐全，先执行：

```bash
SKILL_DIR="${CODEX_HOME:-$HOME/.codex}/skills/ah-douyin-clean-downloader"
python3 "$SKILL_DIR/scripts/doctor.py" --format text
```

自检不会访问抖音，也不会下载视频。

## 下载

默认输出到 `~/Desktop/抖音无水印视频/<博主昵称>/`。

```bash
SKILL_DIR="${CODEX_HOME:-$HOME/.codex}/skills/ah-douyin-clean-downloader"
python3 "$SKILL_DIR/scripts/download_douyin.py" "<用户提供的完整口令或链接>"
```

只有用户明确指定其他媒体库根目录时，才增加：

```bash
--output-dir "/绝对路径"
```

用户明确提供代理时，使用 `--proxy`。也可读取用户已配置的 `AH_DOUYIN_PROXY`，但不得修改系统代理。

## 逐字稿

逐字稿模式需要本机存在 `ffmpeg`、`mlx_whisper` 和可用的 Whisper 模型：

```bash
SKILL_DIR="${CODEX_HOME:-$HOME/.codex}/skills/ah-douyin-clean-downloader"
python3 "$SKILL_DIR/scripts/transcribe_douyin.py" "<用户提供的完整口令或链接>"
```

脚本把下载视频和机器识别稿放在受控临时目录，返回机器稿、机器 SRT 及两份最终成品路径。随后必须：

1. 完整读取机器识别稿。
2. 结合标题和上下文修正同音词、专有名词、断句、标点及明显口误，不新增原视频没有的观点。
3. 将完整正文写入脚本返回的 `final_path`。
4. 保留机器 SRT 的序号和时间码，只校对每段字幕文字，并写入 `final_srt_path`。Markdown 与 SRT 的术语和内容必须一致。
5. 抽查开头、中段、结尾，确认两个成品都不是截断稿或空稿。
6. 用脚本清理其返回的 `work_dir`：

```bash
python3 "$SKILL_DIR/scripts/transcribe_douyin.py" --cleanup-work-dir "<work_dir>"
```

最终交付目录只允许保留校对后的逐字稿 `.md` 和逐段时间码 `.srt` 两份文件。JSON、TXT、临时视频及其他识别文件必须清理。清理失败时必须报告残留路径，不能声称已经只保留成品。

## 成功回报

脚本成功时输出 JSON。向用户报告：

- 最终文件的绝对路径。
- 博主分类文件夹的绝对路径。
- 文件大小和时长。
- 检测到的视频流和音频流数量。
- 视频未经转码，保留下载源中的原始音视频。

逐字稿模式只报告：

- 校对后的逐字稿绝对路径。
- 校对后的 SRT 时间轴绝对路径。
- 视频时长和博主名。
- 临时过程文件是否已经清理。

## 约束

- 只接受 `douyin.com` 和 `iesdouyin.com` 的官方分享链接。
- 只选择 `play_addr_h264`、`play_addr` 或 H.264 码率播放源，不选择 `download_addr`。
- 不保存 Cookie、账号、口令或密钥，不修改系统代理。
- 不覆盖现有同名文件；脚本会自动增加序号。
- 平台水印与作者硬水印必须区分。本 Skill 获取不带抖音平台角标的播放源，但不会擦除作者上传前已烧录进画面的 Logo、字幕或贴片。
- 博主昵称缺失时使用 `未知博主` 文件夹。
- 博主昵称会用于本地路径，必须使用脚本的路径安全清理结果，不手动拼接未清理的昵称。
- 机器识别稿不是最终交付；必须完成语义校对后再向用户报告成功。
- SRT 只修正字幕文字，不改变原始时间码；需要重新切分字幕时必须重新核对对应语音。

平台接口、网络和维护说明见 [references/backend.md](references/backend.md)。
