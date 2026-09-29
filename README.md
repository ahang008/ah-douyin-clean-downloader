# ah-douyin-clean-downloader

同一个 Codex Skill 下载单条抖音原视频、交付校对后的单条逐字稿与 SRT、归档博主公开作品并生成本地机器稿，也能把已有完整资料库按点赞排序批量改写成口播稿。

这是 [阿杭 Skills](https://github.com/ahang008/ah-skills) 旗下的独立仓库，本分支功能版本为 **v0.4.0**。仅用于你自有或已获授权的公开内容。

## 按请求选择流程

| 你发给 Codex 的内容 | 默认行为与结果 |
| --- | --- |
| 单条官方视频链接或完整分享口令 | 下载平台播放源原视频，保留原始音视频，不转码。 |
| 单条链接，并说明“提取逐字稿” | 本地识别，再完成整篇语义校对；最终只保留校对后的 Markdown 和原时间码 SRT，清理受控过程文件。 |
| 博主主页、分享名片，或“全部作品/整号下载转写” | 官方网页 SDK 验证公开目录并采集可用指标，保存原片与机器 Markdown/SRT/识别证据，按指标命名，生成指标表、索引、作者合集与 JSONL，可续跑。 |
| 已有完整资料库，并要求“全部洗稿/批量改写” | 按点赞稳定排序，准备写作分片；Codex 逐篇重建正文，再另存口播稿、来源与开头时间映射，生成动态 N 篇索引、合集和核对清单。 |

原视频来自平台播放源，不能擦除作者上传前已烧录的 Logo、字幕或贴片。批量机器稿未经逐篇人工听音校对；语义校对和改写使用额外模型用量，另存成品，保留原片、原稿和 SRT。

## 直接交给 Codex 安装

把下面这句话发给 Codex：

```text
安装这个 Skill：https://github.com/ahang008/ah-douyin-clean-downloader
```

仓库根目录就是 Skill 目录。内置 `skill-installer` 的标准安装命令保持不变：

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-installer/scripts/install-skill-from-github.py" \
  --repo ahang008/ah-douyin-clean-downloader \
  --path . \
  --name ah-douyin-clean-downloader
```

安装到 `~/.codex/skills/ah-douyin-clean-downloader/`，新开一轮 Codex 对话即可使用。无需安装第二份整号或改写 Skill。内置安装器遇到已存在的同名目录会拒绝覆盖；上述命令用于新安装，不是本机升级命令。更新已有安装时先比较源文件，保留本机私有运行数据。

要新安装尚未合并的 v0.4.0 开发分支，在标准命令中指定 ref：

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-installer/scripts/install-skill-from-github.py" \
  --repo ahang008/ah-douyin-clean-downloader \
  --ref feat/creator-batch-offline \
  --path . \
  --name ah-douyin-clean-downloader
```

也可直接克隆：

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
git clone --depth 1 https://github.com/ahang008/ah-douyin-clean-downloader.git   "${CODEX_HOME:-$HOME/.codex}/skills/ah-douyin-clean-downloader"
```

## 环境与首次准备

| 模式 | 环境 |
| --- | --- |
| 仅下载单条原视频 | Python 3.9+、curl；支持 macOS、Linux 和带这些工具的 Windows。ffprobe 可选，用于完整媒体检查。 |
| MLX 转写与整号批量 | Apple Silicon Mac、macOS 15+、Python 3.12、curl、ffmpeg/ffprobe；固定依赖安装在本 Skill 的独立虚拟环境。 |
| 整号官方页面采集 | 上述批量环境，再加已安装的 Microsoft Edge 和可选 Playwright 运行时。 |
| 已有库的改写准备、渲染与检查 | Python 3.9+，不重新下载、识别或登录；写作与语义审核由 Codex 完成。 |

仅下载无需安装 MLX 或浏览器依赖。环境自检：

```bash
SKILL_DIR="${CODEX_HOME:-$HOME/.codex}/skills/ah-douyin-clean-downloader"
python3 "$SKILL_DIR/scripts/doctor.py" --format text
```

要使用本地转写，先准备 curl、ffmpeg/ffprobe 和 Python 3.12，再运行：

```bash
"$SKILL_DIR/scripts/bootstrap.sh"
"$SKILL_DIR/.venv/bin/python" "$SKILL_DIR/scripts/transcribe_local.py" --doctor
```

模型缺少时显式准备一次：

```bash
"$SKILL_DIR/.venv/bin/python" "$SKILL_DIR/scripts/transcribe_local.py" --download-model
```

整号采集另运行 `scripts/bootstrap-browser.sh`。它只安装可选控制依赖并检查已有 Edge，不下载替代浏览器。首次安装依赖和下载模型需要网络；已缓存模型的识别在本机执行。

## 使用与续跑

单条下载默认按博主分类保存到 `~/Desktop/抖音无水印视频/<博主昵称>/`，昵称缺失时使用 `未知博主`：

```bash
python3 "$SKILL_DIR/scripts/download_douyin.py" "<完整官方视频链接或口令>"
```

指定媒体库根目录加 `--output-dir`；使用用户明确配置的代理加 `--proxy` 或设置 `AH_DOUYIN_PROXY`，不修改系统代理。下载成功 JSON 包含文件路径、大小及实际测得的媒体信息。

单条逐字稿：

```bash
"$SKILL_DIR/.venv/bin/python" "$SKILL_DIR/scripts/transcribe_douyin.py"   "<完整官方视频链接或口令>"
```

脚本先返回机器稿和两份成品路径；Codex 必须完成整篇校对、同步 SRT 文字并验收后，清理返回的 `work_dir`。细则见 [单条逐字稿](references/single-transcript.md)。

博主公开作品批量：

```bash
"$SKILL_DIR/.venv/bin/python" "$SKILL_DIR/scripts/run_creator.py"   "https://www.douyin.com/user/<sec_uid>"   --root "/absolute/path/creator-library"   --browser-session
```

这是默认采集路线，`--browser-session` 可显式保留。用户在专用正常 Edge 官方窗口普通扫码并在手机确认登录，随后脚本自动分页、下载、本地识别和合并，不需要模型 Computer Use。同一命令和同一资料库可中断后续跑；已有验证文件跳过。需要采集新作品时加 `--refresh-catalog`。

批量原片、机器稿、SRT 和证据永久保留；成功后自动导出 `<作者>-N条机器逐字稿合集.md` 与 `catalog/transcripts-N.jsonl`。完整参数、目录结构和验收见 [整号批量流程](references/creator-batch.md)。不要把单条模式的清理规则用于批量资料库。

批量文件名统一为 `赞1234_评56_藏78_转90_视频标题-视频ID.mp4/.md/.srt`。缺失计数使用“未获取”，真实零保留为 `0`；各作品的采集时间、可用性和其他公开指标写入目录、JSONL 与 `catalog/作品数据指标.csv`。指标会随时间变化，文件名代表保存的观察快照。仅刷新指标时运行原命令并加 `--stage collect --refresh-catalog`，即可更新已有文件名、索引和完整合集，无需重新下载或转写。

改名在下载和识别停止写入后执行，同步各路径记录并保留源文件校验值；中断后先恢复改名，再续跑。旧目录没有指标时仍能读取，完整刷新后可升级命名。公开分页完整不代表每项数据均可用；网页返回的播放占位零视为未核实。作者后台的完播率、转化等数据需要另有合法数据来源，当前流程不会补造。

## 已有资料库批量改写

直接告诉 Codex：“把这个已完成资料库全部改写成我的口播稿，保留完整开场，按点赞从高到低交付。”本 Skill 会读取 [批量改写说明](references/batch-rewrite.md)，验证原库并把工作状态和成品放在不同目录：

```bash
python3 "$SKILL_DIR/scripts/batch_rewrite.py" prepare \
  --root "/absolute/path/creator-library" \
  --work-dir "/absolute/path/rewrite-work" \
  --output-dir "/absolute/path/rewrite-output" \
  --shards 3 \
  --persona "阿杭"
```

`--catalog FILE` 可指定目录文件；默认优先读取 `ROOT/catalog/browser-catalog.json`，否则读取 `ROOT/catalog/catalog.json`。`--shards` 按实际批量和可用并行调整，作品数 N 来自原库。准备过程只读取已验证的原片、机器稿、SRT 与证据，不下载、不识别、不登录。

Codex 读取各分片，决定开场需要连续保留多少个 ASR 片段，在 `WORK/drafts-*.jsonl` 写新正文与必要核对。开场按完整语意锁定，通常约 5 秒，以实际内容为准；不按秒数、句号或固定片段数截断。正文承接原题材、语气和开场逻辑，重建为自己的 2–3 个核心层次与新案例。原作者的学员案例/履历、收入、负债、减肥经历不自动成为改写者事实；虚构示例写明“比如”，锁定开场中的未确认亲历单列待核，只影响相关稿。

```bash
python3 "$SKILL_DIR/scripts/batch_rewrite.py" render \
  --work-dir "/absolute/path/rewrite-work" \
  --drafts "/absolute/path/rewrite-work/drafts-1.jsonl" "/absolute/path/rewrite-work/drafts-2.jsonl" "/absolute/path/rewrite-work/drafts-3.jsonl"
python3 "$SKILL_DIR/scripts/batch_rewrite.py" check \
  --work-dir "/absolute/path/rewrite-work"
```

完整渲染默认必须覆盖每个源视频 ID 且只出现一次。输出为 `001_赞…_评…_藏…_转…_标题-ID/01-洗稿口播稿.md`，带完整开场、正文、必要拍摄建议、首评、最多 3 条具体核对、来源和开头时间映射；全量时生成 `00-N篇口播稿合集.md` 和索引。`--partial` 用于明确交付未完成范围，不生成完整合集。已有手改成品不会被静默覆盖；源库漂移时建立新的工作目录。已有库默认不重新采集，用户明确要求最新指标时可先完整刷新，再创建新的 WORK 快照。

主排序为点赞降序，并列依次比较分享、收藏、评论、发布时间降序和 ID 升序；未获取计数排在已知值后。辅助参考分仅对同批四项指标完整的作品计算，使用平均并列分位：赞 40%、转 30%、藏 20%、评 10%。缺项不填 0、不出完整分；辅助分只供编辑选稿，不代表播放量或平台算法。播放量未获取时绝不估算。

机械验证能检查覆盖、顺序、锁定开头、文件可读性和来源一致性；逐篇语义、事实和听音须单独完成。全篇读取并核验衔接、开场承诺与结尾，重复检测只是复核提示。创作和语义审核仍消耗模型用量，不能称“洗稿零 Token”。在 Obsidian 项目中，按项目路由保存任务记录、索引与稿件；媒体、缓存和 WORK 状态进入 `_工具/`，用户指定路径优先。

## 完成与隐私

`catalog_complete=true`、两个全量保存标志均为 true、缺失 ID 为空且文件校验通过，才可称当前公开视频范围完成。官方首页计数、样本下载成功或旧合集均不足以证明全量；机器稿完成也不代表已人工校对。

维护实测验证过连续官方分页、完整原片与机器稿/SRT/证据、续跑校验跳过和正常退出。作品数与导出数量以每次实际资料库为准。公开仓库不包含真实媒体、全文、登录截图或浏览器资料目录。

机械批量脚本不调用付费解析、托管 ASR、在线 LLM 或模型 Computer Use。单条语义校对、批量创作、语义审核和 Codex 对话本身仍使用模型用量。下载和模型准备需要网络；本地识别消耗计算、内存、磁盘和电量。文件技术验收不认证内容语义、事实和听音质量。

专用 Edge 会正常保存本次登录；`.browser-session/` 和登录截图属于私有运行数据，不随 Skill 打包、不公开分享。流程不读取或复制日常浏览器会话，不修改系统代理，不削弱浏览器沙箱、证书校验或密码存储。

## 维护

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/*.py tests/*.py
```

回归使用合成数据和模拟识别，不需要真实账号、钥匙串或用户私密内容。故障排查见 [troubleshooting.md](references/troubleshooting.md)，后端边界见 [backend.md](references/backend.md)，版本变化见 [CHANGELOG.md](CHANGELOG.md)。项目来源见 [PROVENANCE.md](PROVENANCE.md) 与 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

本项目保持源码可见、非商业使用许可证，允许个人学习、研究和非商业使用；未经书面授权不得用于商业用途，不属于 OSI 认可的开源许可证。完整条款见 [LICENSE](LICENSE)。

其他独立 Skill 见 [ahang008/ah-skills](https://github.com/ahang008/ah-skills)。
