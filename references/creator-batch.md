# 博主公开作品批量保存与本地转写

用于博主官方主页、分享名片、`sec_uid`，或明确的整号/全部作品请求。只处理用户自有或已获授权的公开作品。默认交付原片和原始机器稿，保留证据；批量全稿语义校对是另一个可选步骤。

## 首次准备

整号与 MLX 识别使用 Apple Silicon Mac、macOS 15+、Python 3.12、curl、ffmpeg/ffprobe，以及已安装的官方 Microsoft Edge。单条下载的 Python 3.9+ 跨平台环境不因此变更。

```bash
SKILL_DIR="${CODEX_HOME:-$HOME/.codex}/skills/ah-douyin-clean-downloader"
"$SKILL_DIR/scripts/bootstrap.sh"
"$SKILL_DIR/scripts/bootstrap-browser.sh"
"$SKILL_DIR/.venv/bin/python" "$SKILL_DIR/scripts/transcribe_local.py" --doctor
```

基础 bootstrap 在独立虚拟环境中安装固定的本地 MLX 依赖，不自行下载模型；浏览器 bootstrap 单独准备可选 Playwright 依赖并检查已安装的 Edge，不下载 Playwright 自带浏览器。缺少本地模型时显式准备一次：

```bash
"$SKILL_DIR/.venv/bin/python" "$SKILL_DIR/scripts/transcribe_local.py" --download-model
```

默认模型为 `mlx-community/whisper-large-v3-turbo`。首次下载公开权重需要网络和磁盘；模型已缓存后，识别只读本地文件，不调用 Hugging Face 推理服务或托管 ASR。

## 运行、续跑和更新

```bash
"$SKILL_DIR/.venv/bin/python" "$SKILL_DIR/scripts/run_creator.py" \
  "https://www.douyin.com/user/<sec_uid>" \
  --root "/absolute/path/creator-library" \
  --browser-session
```

`profile` 也可传完整官方分享名片或 `sec_uid`。`--root` 是持久化资料库，使用同一目录续跑。未显式传 `--browser-session` 时仍使用同一官方浏览器采集路线。

首次打开专用 Edge 官方页面时，用户用手机抖音“扫一扫”扫描二维码，并在手机确认登录或按平台提示完成验证；二维码失效时先点“点击刷新”。聊天中的“同意”只是授权，不是登录已完成的证据。模型无需 Computer Use。

网页 SDK 发出目标作者分页，脚本采集公开响应的必要字段和游标证据。完整目录会缓存；中断后执行同一命令，已通过源文件、输出与设置校验的原片和机器稿会跳过。运行中的同一资料库无需重复启动。

要纳入新作品，在原命令加 `--refresh-catalog`。采集重新从游标 0 建链，完整后更新主目录；失败保留已有目录和已完成文件，不能用旧目录替本次失败宣称刷新成功。

默认并行下载与单路本地识别；下载收尾后会重新读取最终媒体清单，处理最后一轮新增的已校验媒体。若终止时仍有不可用媒体或识别失败，报告未完成并以非零状态退出，保留成果供续跑。

## 参数

| 参数 | 用途 |
| --- | --- |
| `profile` | 官方主页、分享名片或 `sec_uid`；已有资料库可按保存的作者信息续跑 |
| `--root DIR` | 必填，持久化资料库目录 |
| `--catalog FILE` | 指定目录文件，默认 `ROOT/catalog/catalog.json` |
| `--stage all` | 默认，采集、下载、本地识别及成功后的合集导出 |
| `--stage collect` | 只采集公开目录 |
| `--stage download` | 只下载目录内视频 |
| `--stage transcribe` | 只处理已有已校验媒体，不需要浏览器 |
| `--stage status` | 读取摘要和检查点，查看当前进度 |
| `--stage doctor` | 检查本地运行条件 |
| `--refresh-catalog` | 重新采集，纳入新公开作品 |
| `--limit N` | 正数为样本模式，0 为全部；样本成功不能称全量完成 |
| `--workers N` | 下载并发，默认 2；本地识别仍单路执行 |
| `--serial` | 下载全部结束后再识别 |
| `--browser-session` | 显式使用默认的专用正常 Edge SDK 采集路线 |
| `--browser-session-dir DIR` | 专用私有资料目录，默认 `ROOT/.browser-session`；不能指定日常 Edge 目录 |
| `--browser-timeout SECONDS` | 浏览器采集总时限，含用户登录等待；默认 600，允许 30–3600 |
| `--model PATH_OR_REPO` | 已缓存的本地 MLX 模型 |
| `--initial-prompt TEXT` | 本地识别的词汇提示，按原样记录，不执行在线改写 |
| `--proxy URL` | 用户明确配置的 HTTP 下载/分享名片解析代理，不修改系统设置 |
| `--dns-mode auto\|off\|always` | 进程内 DNS 策略；auto 在官方域解析失败或返回假地址时启用浏览器本地通道，off 直连，always 强制通道 |
| `--backend FILE` | 覆盖视频下载后端，通常无需指定，本 Skill 自带下载器 |

不需要匿名签名采集、日常浏览器 Cookie 或钥匙串读取路线。

## 输出与合并合集

```text
creator-library/
  catalog/catalog.json              作者公开目录与分页链
  catalog/browser-pages-*/          SDK 公开字段投影与游标证据
  catalog/catalog-capture-*.json     未完成的新采集
  catalog/catalog-attempt-*.json     采集失败记录
  catalog/transcripts-N.jsonl        N 条机器正文与本地来源映射
  media/<作者>/*.mp4                下载源原片，保留
  media/download-manifest.json       逐条下载状态、SHA256、时长和音视频检查
  local-transcripts/<视频ID>/
    01-本地ASR机器逐字稿.md
    01-本地ASR机器逐字稿.srt
    01-本地ASR识别证据.json          原始文本、片段、置信提示和源文件/模型信息
  local-transcripts/_batch-state.json
  <作者>-N条机器逐字稿合集.md         按实际条数合并的完整机器正文
  视频与逐字稿索引.md                原片、机器稿、字幕的本地链接
  pipeline-summary.json
  pipeline-run.json
  logs/collect.log
  logs/download.log
  logs/transcribe.log
  .browser-session/                 私有浏览器运行数据，不打包或公开分享
```

全量流程成功后自动调用 `scripts/export_transcripts.py`，按当前目录中实际通过校验的 N 条视频生成泛型作者合集与 JSONL。原片、单篇 Markdown、SRT 和证据保留；合集不替代逐篇记录。不要把样本合集或旧合集当作全量验收。

已完成资料库可单独重建合集，不重新下载或识别：

```bash
"$SKILL_DIR/.venv/bin/python" "$SKILL_DIR/scripts/export_transcripts.py" \
  --root "/absolute/path/creator-library"
```

使用自定义目录文件时加 `--catalog FILE`；`--check` 只报告是否就绪，不写合集；`--wait` 等待已有任务完成，不启动新任务。`--expected-count N` 是附加数量断言，不能代替真实完整分页链。需要自定义成品路径时使用 `--markdown-output`、`--jsonl-output`。

## 完成验收

以当前 `pipeline-summary.json` 和实际逐条文件校验为准，同时确认：

- `catalog_complete=true`：目标作者从请求游标 0 开始连续分页到真实 `has_more=0`，作者一致，无登录过滤，视频清单非空。主页数字、HTTP 200、窗口出现或数量达到阈值均不够。
- `all_public_videos_downloaded=true`：目录内每个视频都有已验证原片、SHA256、正时长与音视频流检查。
- `all_public_videos_transcribed=true`：每个视频均有非空机器 Markdown、SRT 和识别证据，源文件/模型/输出校验通过，正文与片段、字幕对应，时间码在技术容差内。
- `missing_download_ids` 与 `missing_transcript_ids` 均为空，未设置正数 `--limit`；导出的 N 与当前完整视频目录一致。

`library_download_verified_total` / `library_transcript_saved_total` 是整个资料库已验证数量，`download_verified` / `transcript_saved` 是当前目录范围，不能互相代替。图文作品、删除/私密或不可访问作品独立记录范围，不能称全部平台作品均已保存。

技术验收证明文件齐全且彼此一致，不证明无错字或无漏识别。低置信、重复概率、尾段对齐等提示仍需结合原片复核；未做逐篇听音时明确写“机器稿，未逐篇人工听音校对”。

维护实测曾取得 8 个连续官方分页的 139 条公开视频，139 原片与 139 套机器 Markdown/SRT/证据全部通过，合计 68,416 正文字符、7,701 字幕段；一键续跑校验跳过并正常退出，收尾竞争的 3 项离线回归通过。这是该次实测计数，不是其他博主的固定数量或文字准确率保证；真实媒体、正文、登录截图与资料目录不放进公开 Skill。

## 私有会话与可选语义校对

专用 Edge 使用正常沙箱、证书校验与密码存储；Playwright 只连接本机回环 CDP。默认 auto 先检查官方域解析，只在失败或假地址时启用本机 HTTPS CONNECT/DNS 通道；通道只转发加密字节，不做 TLS 解密，不改系统代理或 DNS。脚本不读取或复制日常 Edge 会话；浏览器会正常保存本次登录，`.browser-session/` 与登录截图属于私有数据，对外分享时排除。

若用户另外要求批量全稿语义校对，完整读取每篇机器稿与对应 SRT，参照 [单条校对细则](single-transcript.md) 保留原意和时间码，另存校对版并检查逐篇覆盖范围。保留批量原片、原始机器稿与证据，不执行单条模式的过程清理。该步骤使用额外 LLM Token；不能用机器文件的全量标志宣称校对版已全量完成。
