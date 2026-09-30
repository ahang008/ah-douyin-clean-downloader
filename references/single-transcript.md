# 单条视频逐字稿

用于单条视频明确要求“提取逐字稿、提取文案、转文字”的请求。先生成机器识别稿，再由 Codex 完成整篇语义校对。机器稿不是该模式的最终交付。

## 环境与运行

使用 Apple Silicon Mac、macOS 15+、Python 3.12、curl、ffmpeg/ffprobe、本地 MLX Whisper。首次准备本地转写环境：

```bash
SKILL_DIR="${CODEX_HOME:-$HOME/.codex}/skills/ah-douyin-clean-downloader"
"$SKILL_DIR/scripts/bootstrap.sh"
"$SKILL_DIR/.venv/bin/python" "$SKILL_DIR/scripts/doctor.py" --format text
"$SKILL_DIR/.venv/bin/python" "$SKILL_DIR/scripts/transcribe_local.py" --doctor
```

模型缺少时，显式运行一次 `"$SKILL_DIR/.venv/bin/python" "$SKILL_DIR/scripts/transcribe_local.py" --download-model`。首次获取公开模型文件需要网络；正常识别只读本地缓存，不使用托管 ASR。运行：

```bash
"$SKILL_DIR/.venv/bin/python" "$SKILL_DIR/scripts/transcribe_douyin.py" \
  "<用户提供的完整官方视频链接或口令>"
```

用户指定成品根目录时加 `--output-dir`，代理加 `--proxy`，模型可用 `--model` 指定。脚本在受控临时目录下载原片、生成机器文本和 SRT，成功返回 `draft_path`、`draft_srt_path`、`final_path`、`final_srt_path`、`work_dir` 等 JSON 字段。按返回路径工作，不自行拼接未清理的博主昵称。

## 整篇校对与验收

1. 完整读取 `draft_path` 与 `draft_srt_path`，按标题和上下文理解整篇内容，不能只校对开头或把正文压缩成摘要。
2. 修正同音词、专有名词、断句、标点及明显口误，保留原意、论证顺序和信息量，不增加原视频没有的观点。无法确定的词保留核对提示，不冒充已经听音确认。
3. 把完整校对正文写入 `final_path`。
4. 保留机器 SRT 的全部序号与时间码，只校对每段字幕文字，写入 `final_srt_path`。Markdown 与 SRT 的术语、内容必须一致；需要重新切分或调整时间码时先核对对应语音。
5. 回读两个成品，检查正文和字幕均非空、无截断、段数与时间码未丢失，抽查开头、中段、结尾。
6. 两份成品验收后，仅用本脚本清理其返回的受控临时目录：

```bash
"$SKILL_DIR/.venv/bin/python" "$SKILL_DIR/scripts/transcribe_douyin.py" \
  --cleanup-work-dir "<脚本返回的 work_dir>"
```

最终成品目录只保留校对后的 `.md` 和 `.srt` 两份文件。JSON、TXT、临时视频、音频与机器识别稿必须清理；清理失败则报告残留路径，不能声称只保留成品。不要删除用户已有原片、其他目录或批量资料库。

## 回报

交付两份校对成品的绝对路径、视频时长与博主名，并说明受控过程文件是否已清理。语义校对使用 Codex 的模型用量；“本地识别无托管 ASR”不表示校对阶段不耗模型 Token。未做逐篇听音时如实标注校对范围。

仅要求总结或分析时，按当次请求取得必要文字并处理，不擅自扩大到整号归档或额外交付整套资料。
