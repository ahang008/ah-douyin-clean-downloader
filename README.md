# ah-douyin-clean-downloader

同一个 Codex Skill 处理三种请求：下载单条抖音原视频、交付校对后的单条逐字稿与 SRT，或归档博主的公开作品并批量生成本地机器稿。

这是 [阿杭 Skills](https://github.com/ahang008/ah-skills) 旗下的独立仓库，当前功能版本为 **v0.3.1**。仅用于你自有或已获授权的公开内容。

## 按请求选择流程

| 你发给 Codex 的内容 | 默认行为与结果 |
| --- | --- |
| 单条官方视频链接或完整分享口令 | 下载平台播放源原视频，保留原始音视频，不转码。 |
| 单条链接，并说明“提取逐字稿” | 本地识别，再完成整篇语义校对；最终只保留校对后的 Markdown 和原时间码 SRT，清理受控过程文件。 |
| 博主主页、分享名片，或“全部作品/整号下载转写” | 官方网页 SDK 验证公开目录并采集可用指标，保存原片与机器 Markdown/SRT/识别证据，按指标命名，生成指标表、索引、作者合集与 JSONL，可续跑。 |

原视频来自平台播放源，不能擦除作者上传前已烧录的 Logo、字幕或贴片。批量机器稿未经逐篇人工听音校对；如果另外要求整批语义校对，会使用额外模型用量并另存校对版。

## 直接交给 Codex 安装

把下面这句话发给 Codex：

```text
安装这个 Skill：https://github.com/ahang008/ah-douyin-clean-downloader
```

仓库根目录就是 Skill 目录。内置 `skill-installer` 的标准安装命令保持不变：

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-installer/scripts/install-skill-from-github.py"   --repo ahang008/ah-douyin-clean-downloader   --path .   --name ah-douyin-clean-downloader
```

安装到 `~/.codex/skills/ah-douyin-clean-downloader/`，新开一轮 Codex 对话即可使用。无需安装另一份整号 Skill。也可直接克隆：

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

## 完成与隐私

`catalog_complete=true`、两个全量保存标志均为 true、缺失 ID 为空且文件校验通过，才可称当前公开视频范围完成。官方首页计数、样本下载成功或旧合集均不足以证明全量；机器稿完成也不代表已人工校对。

维护实测验证过 8 个连续官方分页、139 条公开视频与 139 套机器稿/SRT/证据，68,416 正文字符、7,701 字幕段，续跑校验跳过并正常退出。公开仓库只保留不含私密内容的计数证据，不包含该次真实媒体、全文、登录截图或浏览器资料目录。

批量脚本不调用付费解析、托管 ASR 或在线 LLM。单条语义校对、可选批量全稿校对和 Codex 对话本身仍使用模型用量。下载和模型准备需要网络；本地识别消耗计算、内存、磁盘和电量。

专用 Edge 会正常保存本次登录；`.browser-session/` 和登录截图属于私有运行数据，不随 Skill 打包、不公开分享。流程不读取或复制日常浏览器会话，不修改系统代理，不削弱浏览器沙箱、证书校验或密码存储。

## 维护

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/*.py tests/*.py
```

回归使用合成数据和模拟识别，不需要真实账号、钥匙串或用户私密内容。故障排查见 [troubleshooting.md](references/troubleshooting.md)，后端边界见 [backend.md](references/backend.md)，版本变化见 [CHANGELOG.md](CHANGELOG.md)。项目来源见 [PROVENANCE.md](PROVENANCE.md) 与 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

本项目保持源码可见、非商业使用许可证，允许个人学习、研究和非商业使用；未经书面授权不得用于商业用途，不属于 OSI 认可的开源许可证。完整条款见 [LICENSE](LICENSE)。

其他独立 Skill 见 [ahang008/ah-skills](https://github.com/ahang008/ah-skills)。
