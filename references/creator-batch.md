# 博主公开作品批量保存与本地转写

用于博主官方主页、分享名片、`sec_uid`，或明确的整号/全部作品请求。只处理用户自有或已获授权的公开作品。默认交付原片、原始机器稿、公开指标、发布文案与选定封面文字，保留证据；批量全稿语义校对是另一个可选步骤。

当前整号 `all` 转写路线仍下载并保留原片，用于本地 ASR 和来源校验；`--stage metadata` 只补标题、标签与封面，既不下载视频，也不生成逐字稿。用户明确“只要逐字稿、不要视频”时，不能把批量 `all` 说成无视频流程；单条转写按 [单条模式](single-transcript.md) 清理过程媒体。

已有库完成后，用户要求全部洗稿或批量重写口播时，转到 [批量改写](batch-rewrite.md)；默认不重新采集、下载或识别，不改原片、机器稿与 SRT。

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

如果已有官方公开目录，只运行 `--stage metadata`，不下载 MP4 或运行 ASR；正常封面请求使用系统 Python 3、`curl`、`sips` 和 `swift`。仅当旧接口没有返回目标封面时，自动开启同一专用 Edge 官方作品页补采，需已准备浏览器依赖。

## 运行、续跑和更新

```bash
"$SKILL_DIR/.venv/bin/python" "$SKILL_DIR/scripts/run_creator.py" \
  "https://www.douyin.com/user/<sec_uid>" \
  --root "/absolute/path/creator-library" \
  --browser-session
```

`profile` 也可传完整官方分享名片或 `sec_uid`。`--root` 是持久化资料库，使用同一目录续跑。未显式传 `--browser-session` 时仍使用同一官方浏览器采集路线。

首次打开专用 Edge 官方页面时，用户用手机抖音“扫一扫”扫描二维码，并在手机确认登录或按平台提示完成验证；二维码失效时先点“点击刷新”。聊天中的“同意”只是授权，不是登录已完成的证据。模型无需 Computer Use。

网页 SDK 发出目标作者分页，脚本采集公开响应的必要字段、可用计数和游标证据。每页记录一次指标观察时间，不保存完整响应中的私人字段。分页里若混有其他作者作品，将其公开 ID、作者和排除原因单列在目录 `excluded_foreign_author_works`，不计入目标作者的视频或图文数；作者身份缺失或格式异常仍拒绝该页。完整目录会缓存；中断后执行同一命令，已通过源文件、输出与设置校验的原片和机器稿会跳过。运行中的同一资料库无需重复启动。完整目录取得后，批量流程还应为每个视频 ID 补采发布文案及官方选定封面，并在本机识别封面文字。

旧 Aweme feed 接口若返回 `status_code=0` 但未包含目标视频 ID，下载和封面阶段会在专用 Edge 打开该作品的抖音官方网页，限定读取 `/aweme/v1/web/aweme/detail/`，同时核对目标 ID 与作者身份。网页播放源通过同一下载器与 ffprobe 验收，随后批次重新核验并接管 MP4；封面从 `video.cover` 取得，缓存来源校验标记后由本地 OCR 处理。失败保留原缺失 ID 和固定错误类别。`catalog/web-detail-media-recovery-report.json` 与 `catalog/web-detail-cover-recovery-report.json` 不写入签名播放链接、Cookie 或完整网页响应；专用 Edge 的登录会话仍按浏览器正常行为保存在私有 `.browser-session/`，不得打包分享。

要纳入新作品，在原命令加 `--refresh-catalog`。采集重新从游标 0 建链，完整后更新主目录；失败保留已有目录和已完成文件，不能用旧目录替本次失败宣称刷新成功。

只更新已有库的公开指标与文件名，在原命令加 `--stage collect --refresh-catalog`。它会采集完整公开目录，再离线同步已有文件名、指标表和索引；已有全部机器稿通过校验时也会重建合集。新出现的视频会记录在目录里，需运行默认全流程才会下载和转写。

默认并行下载与单路本地识别；下载收尾后会重新读取最终媒体清单，处理最后一轮新增的已校验媒体。机器时间码若只有局部倒序，流程先保存原始失败结果，再用原片附近不超过 30 秒的音频做一次本地词级对齐；只在相邻三段文字完全匹配且时间改动有界时修复时间码，不改机器识别用词。续跑优先复用源视频、音频及模型设置均匹配的原始失败结果，避免重识别整条长视频。若终止时仍有不可用媒体、识别失败或机器输出需听核，报告未完成并以非零状态退出，保留成果供续跑。

质量关拦下短片后，执行者先自行核对原片证据：定位可能的人声窗口、用独立本地识别结果交叉检查，并按需抽取画面文字。能以原音频和一致文字修复局部时间码时，保留旧稿与修复证据后再更新机器稿；整段反复幻觉或无可交叉确认的口播时，保留原片、机器失败记录和画面文字摘录，写明可用内容与未解决边界。不要把常规排查直接转交用户，也不要把画面文字冒充口播逐字稿；没有实际听音时不能写“已人工听核”或断言绝对无口播。

## 参数

| 参数 | 用途 |
| --- | --- |
| `profile` | 官方主页、分享名片或 `sec_uid`；已有资料库可按保存的作者信息续跑 |
| `--root DIR` | 必填，持久化资料库目录 |
| `--catalog FILE` | 指定目录文件，默认 `ROOT/catalog/catalog.json` |
| `--stage all` | 默认，采集、下载、本地识别及成功后的合集导出 |
| `--stage collect` | 采集公开目录和指标，并同步已有文件名、索引及就绪合集；不下载或识别新作品 |
| `--stage metadata` | 仅对现有目录补采发布文案、标签及封面静帧并做本地 OCR；不下载 MP4 或转写 |
| `--stage download` | 只下载目录内视频 |
| `--stage transcribe` | 只处理已有已校验媒体，不需要浏览器 |
| `--stage status` | 读取摘要和检查点，查看当前进度 |
| `--stage doctor` | 检查本地运行条件 |
| `--refresh-catalog` | 重新采集公开作品及其指标快照 |
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
  catalog/作品数据指标.csv            指标、观察时间、可用性与本地路径
  catalog/作品标题标签封面.jsonl      逐作品发布文案、标签、封面 OCR 与来源/状态
  catalog/cover-metadata-report.json 目录覆盖、OCR 状态和样本标记
  covers/<视频ID>.jpg                抖音选定封面静帧，不是视频首帧
  标题标签封面索引.md                标题候选、标签、封面文字及复核状态
  media/<作者>/赞1234_评56_藏78_转90_标题-ID.mp4
  media/download-manifest.json       逐条下载状态、SHA256、时长和音视频检查
  local-transcripts/<视频ID>/
    赞1234_评56_藏78_转90_标题-ID.md
    赞1234_评56_藏78_转90_标题-ID.srt
    01-本地ASR识别证据.json          原始文本、片段、置信提示和源文件/模型信息
  local-transcripts/_batch-state.json
  local-transcripts/_failed/<视频ID>/*-raw-invalid.json  原始识别失败结果；局部修复另有对齐证据
  annotated-transcripts/<视频ID>/<原机器稿文件名>.md  标注在前的阅读副本
  annotated-transcripts/_generated-state.json        生成记录与手改保护
  annotated-transcripts/annotation-report.json       已生成、缺失及状态计数
  <作者>-N条机器逐字稿合集.md         按实际条数合并的完整机器正文
  视频与逐字稿索引.md                原片、机器稿、字幕的本地链接
  pipeline-summary.json
  pipeline-run.json
  _artifact-name-journal.json        仅改名中断时存在，续跑先恢复
  logs/collect.log
  logs/download.log
  logs/transcribe.log
  logs/metadata.log
  logs/annotate.log
  .browser-session/                 私有浏览器运行数据，不打包或公开分享
```

全量流程成功后自动调用 `scripts/export_transcripts.py`，按当前目录中实际通过校验的 N 条视频生成泛型作者合集与 JSONL。原片、单篇 Markdown、SRT 和证据保留；合集不替代逐篇记录。不要把样本合集或旧合集当作全量验收。

### 标题、井号标签与封面文字

批量 `all` / `collect` 阶段会从当前公开目录按视频 ID 补采作品信息。已有完整目录只需补这一层、无需重下视频或重做 ASR 时，可运行 `--stage metadata`，或直接调用：

```bash
python3 "$SKILL_DIR/scripts/run_creator.py" \
  --root "/absolute/path/creator-library" --stage metadata

python3 "$SKILL_DIR/scripts/collect_cover_metadata.py" \
  --catalog "/absolute/path/creator-library/catalog/catalog.json" \
  --root "/absolute/path/creator-library"
```

需要代理时加 `--proxy URL`；`--limit N` 只处理前 N 条，JSONL 和索引仍列全目录，未处理项为 `not_attempted`。样本退出成功仅代表选中条目有终态，不得报告为全量。脚本复用已缓存的封面 JPEG 和完成记录；成功时输出 `catalog/作品标题标签封面.jsonl`、`catalog/cover-metadata-report.json`、`covers/<视频ID>.jpg` 与 `标题标签封面索引.md`。这些是作品静帧与文字数据，不是视频。

较早的资料库若只保存 `catalog/browser-catalog.json`，`--stage metadata` 会自动读取它；直接运行采集脚本时，把示例中的 `--catalog` 路径改为该文件。`--stage status` 同样能读取这个旧文件名。

`published_caption` 保留完整发布文案；`title_candidate` 是第一个井号标签之前的文字，文案从标签开始时为 `null`，不能凭文件名或口播补造标题。`hashtags` 按首次出现顺序保留 `#` 并去重；没有标签是空数组。封面只取作品 `video.cover` 的选定图片，不拿 `origin_cover` 或视频开头画面冒充封面。JSONL 同时保存 `official_url`、`caption_source`、`catalog_sha256`、`cover_source`、`cover_captured_at_utc`、`cover_image_path`、`cover_text_raw`、`ocr_lines`、`cover_status` 和 `human_verified`；本地 OCR 的 `human_verified=false`，它只是待核文本。

`cover_status=ok` 表示 OCR 找到文字，仍非逐字人工验收；`ocr_low_confidence` 应显式标为待核；`ocr_empty` 表示封面已取得但机器未读出文字，不能写成“确认无字”；获取或识别失败应保留具体失败状态、空文字和可用的图像路径以便续跑，不把它混入“无文字”。索引中的文字与状态必须成对呈现；重要标题据原封面核对后才可标人工确认。批量报告分别给出目录覆盖、文案字段覆盖、封面取得数、OCR 各状态数，不能因为正文转写成功就声称封面也全量核对。

缓存 JPEG 本身不能证明来自旧 feed 还是官方网页详情。缺少可信来源标记或标记中的图片 SHA-256 与现有 JPEG 不一致时，重做本地 OCR 并将 `cover_source` 记为 `cached_cover_source_unverified`；不得自动写成 feed 来源，需重新获取官方图片或人工核查来源。

已有已校验的机器 Markdown 时，`all`、`collect`、`metadata` 和 `transcribe` 阶段会在元数据可用后生成逐条 `annotated-transcripts/` 阅读副本：文件开头标注标题候选、标签、封面原图链接、OCR 文字与状态，后面逐字节接原机器稿。原 `local-transcripts/` 文件和 SRT 不改；没有机器稿时只报告缺失，不伪造逐字稿。生成器记录自己的输出校验值，重跑会跳过未变化的副本，遇到人工改过的副本则拒绝覆盖。仅需离线重建阅读副本时可运行：

```bash
python3 "$SKILL_DIR/scripts/build_annotated_transcripts.py" \
  --root "/absolute/path/creator-library" \
  --catalog "/absolute/path/creator-library/catalog/catalog.json"
```

如元数据使用自定义目录，再加 `--cover-metadata FILE`；要与生成元数据所用的目录文件保持同一快照。`annotation-report.json` 里的实际副本数、`metadata_missing` 和 `metadata_not_attempted` 要单独核对；没有可用机器稿而脚本正常退出，不等于已完成逐字稿标注。样本 `--limit` 运行后，未处理作品不会新建阅读副本，不能把样本验收当作全量标注。

### 指标与文件名

默认名称为 `赞{点赞}_评{评论}_藏{收藏}_转{分享}_{标题}-{视频ID}`，MP4、Markdown 和 SRT 使用相同 stem。这里的“转”使用公开 `share_count`；另有 `forward_count` 时独立保留。非法文件名字符会清理，过长标题按 UTF-8 字节截短，视频 ID 始终保留。

计数只接受有效的非负整数。字段缺失或无效写 `null`，文件名和表格显示“未获取”；真正返回的互动计数 `0` 仍为 `0`。其他公开字段包括播放、转发、下载计数，是否可用逐项记录；播放占位零记录为 `zero_unverified`，不作为真实播放 0。数据来自官方网页公开观察，不能据此声称作者后台的完播率、转化、收入等信息已取得。

`statistics_captured_at` 为 UTC 观察时间，`statistics_availability` 说明每个字段的可用性。摘要的 `public_metrics` 分别统计各字段已获取与未知数量，不能把 `catalog_complete=true` 理解为所有指标齐全。计数变化需要显式完整刷新；未完成的新采集不会替换旧指标或旧文件名。

实际改名在所有媒体写入者退出后进行，更新媒体清单、识别证据的媒体路径和输出路径，保留原片、正文与字幕的字节及校验值。识别证据保持固定名称，视频 ID 子目录稳定；读取旧目录时仍支持原 `01-本地ASR机器逐字稿.md/.srt`。改名恢复记录用于中断后的前滚恢复，同一命令可继续完成，不需要重新识别。机器稿正文仍保留原生成时元信息，最新文件链接以索引与 JSONL 为准。

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
- `quality_review_ids` 为空：没有被短片重复句、单段周期重复、极少文字、长空尾或稀疏长段质量关拦下的机器输出。若有值，原始机器文件仍保留供听核，但不计入 `transcript_saved`、完整合集或 `all_public_videos_transcribed`；`machine_draft_file_total` 单列已有机器文件数。质量关只提示需听核，不证明视频没有口播。
- `cover_metadata_complete=true`：本次完整目录内每个视频都有对应的发布文案及封面处理记录，封面状态为 `ok`、`ocr_low_confidence` 或 `ocr_empty`；同时检查 `cover_metadata_missing_ids` 为空。这个标志只证明采集与机器识别结束，不证明封面文字正确。
- `missing_download_ids` 与 `missing_transcript_ids` 均为空，未设置正数 `--limit`；导出的 N 与当前完整视频目录一致。

`library_download_verified_total` / `library_transcript_saved_total` 是整个资料库已验证数量，`download_verified` / `transcript_saved` 是当前目录范围，不能互相代替。图文作品、删除/私密或不可访问作品独立记录范围，不能称全部平台作品均已保存。

技术验收证明文件齐全且彼此一致，不证明无错字或无漏识别。低置信、重复概率、尾段对齐等提示仍需结合原片复核；未做逐篇听音时明确写“机器稿，未逐篇人工听音校对”。

维护实测曾取得 8 个连续官方分页的 139 条公开视频，139 原片与 139 套机器 Markdown/SRT/证据全部通过，合计 68,416 正文字符、7,701 字幕段；一键续跑校验跳过并正常退出，收尾竞争的 3 项离线回归通过。这是该次实测计数，不是其他博主的固定数量或文字准确率保证；真实媒体、正文、登录截图与资料目录不放进公开 Skill。

## 私有会话与可选语义校对

专用 Edge 使用正常沙箱、证书校验与密码存储；Playwright 只连接本机回环 CDP。默认 auto 先检查官方域解析，只在失败或假地址时启用本机 HTTPS CONNECT/DNS 通道；通道只转发加密字节，不做 TLS 解密，不改系统代理或 DNS。脚本不读取或复制日常 Edge 会话；浏览器会正常保存本次登录，`.browser-session/` 与登录截图属于私有数据，对外分享时排除。

若用户另外要求批量全稿语义校对，完整读取每篇机器稿与对应 SRT，参照 [单条校对细则](single-transcript.md) 保留原意和时间码，另存校对版并检查逐篇覆盖范围。保留批量原片、原始机器稿与证据，不执行单条模式的过程清理。该步骤使用额外 LLM Token；不能用机器文件的全量标志宣称校对版已全量完成。
