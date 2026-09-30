# 已有资料库批量改写口播稿

用于用户要求将已完成的博主资料库“全部洗稿、批量改写、按点赞交付”。本 Skill 自行完成写作和语义审核，无需另一份改写 Skill。单条下载、逐字稿校对和新建整号资料库仍按入口各自的路线执行。

## 资料与目录

- 原库 LIB 必须有完整公开目录、已验证原片、机器 Markdown、SRT 与 ASR 证据。`prepare` 复用导出器的离线完整性验证；默认不重新采集，不下载、不重新识别、不登录、不调用在线 LLM 或模型 Computer Use。用户明确要求最新指标时，先按整号路线完整刷新，再创建新的 WORK 快照；旧快照保留，不掩盖变化。
- WORK 保存运行状态、写作输入、JSONL 草稿和检查报告；OUT 单独保存口播成品。两者独立于原库，不能把成品写回机器稿、字幕或原片路径。
- 用户指定路径优先。在 Obsidian 中按项目 `AGENTS.md` 路由：视频项目保存任务记录、成品与索引，程序、媒体、缓存和 WORK 状态放在 `_工具/`。未指定项目时默认走视频制作入口，不在桌面另建散落稿件。
- `--catalog` 默认优先用 `LIB/catalog/browser-catalog.json`，不存在时用 `LIB/catalog/catalog.json`；可明确指定已完成目录文件。

```bash
SKILL_DIR="${CODEX_HOME:-$HOME/.codex}/skills/ah-douyin-clean-downloader"
python3 "$SKILL_DIR/scripts/batch_rewrite.py" prepare \
  --root "/absolute/path/creator-library" \
  --catalog "/absolute/path/creator-library/catalog/browser-catalog.json" \
  --work-dir "/absolute/path/rewrite-work" \
  --output-dir "/absolute/path/rewrite-output" \
  --shards 3 \
  --persona "阿杭"
```

目录文件使用默认位置时省略 `--catalog`。分片数按实际规模和可用并行调整，不固定作品数或每片篇数。

协调者只读 `WORK/run.json` 和 CLI 进度摘要确定数量、分片与剩余范围，`writing_guidance` 提醒写作者只需读取一次本批量参考。每个写作者仅读分配给自己的 `WORK/inputs-*.jsonl`，不把全量 `work-items.jsonl` 或全文合集重复读进每个 agent。`work-items.jsonl` 是可回读核对状态，仅按需查具体条目；出现某条疑点才读取其完整原始证据。写作者从分片中使用原稿和 ASR 片段，避免重复载入同一素材。

`opening_window_seconds` 只是分片预览范围，不是开场长度上限；预览不足以覆盖完整首段时，从 `full_segments_path` 指向的 `work-items.jsonl` 按视频 ID 读取本条连续片段，不载入全量文件。

优先利用已完成 `drafts-*.jsonl`、`rendered-manifest.jsonl` 和 CLI 摘要续作缺失条目。不为正常排序、文件命名、复制锁定开场或 SHA 验证逐篇调用模型；这些工作交给机械工具，模型用量用于创作与必要语义核验。

## 排序与辅助分

主排序固定为点赞降序；并列依次按分享、收藏、评论、发布时间降序，最后视频 ID 升序。每项未获取值放在已知值后，真实 0 与未获取分开。序号来自这一稳定顺序，不按辅助分重排交付目录。三位序号示例不是作品数上限。

辅助 `reference_score` / `reference_rank` 只供编辑选稿。仅同批点赞、分享、收藏、评论四项均有效的作品参加计算。每项采用同值平均秩的分位；完整子集有 M 篇时：

```text
M > 1: 分位 = (小于该值的篇数 + (同值篇数 - 1) / 2) / (M - 1)
M = 1: 分位 = 0.5
辅助分 = 100 × (点赞分位×40% + 分享分位×30% + 收藏分位×20% + 评论分位×10%)
```

同分按主排序稳定打破。缺项不填 0，不改成按剩余权重计算，不输出完整辅助分。保留采集时间和字段可用性；该分不代表播放量、真实流量、作者后台指标或平台算法。播放量未获取时绝不估算。

## 写作：锁定完整开场，重新组织正文

使用分配输入里的原稿和连续 ASR 片段，理解本条的题材、观众、语气、开场吸引力及后续承诺；仅有具体疑点时再回读原库完整证据。模型决定 `opening_segment_count=N`：取从第一个片段开始的连续 N 段，覆盖第一个完整开场语意段。通常在约 5 秒附近，以实际语意完整为界，不按秒数、句号或固定 N 截断。源片段与时间码必须可回读。

锁定开场逐字保留，不悄悄修正机器错字。发现疑似错字时，核对项写出具体原词、时间范围和需回听的问题；无疑点记录也不代表已经听音。用户明确要求改开头时按当次要求另存版本，不能通过草稿正文静默替换锁定文本。

授权使用作者素材，不能证明改写者具有作者履历。原作者的学员案例或自述履历、收入、负债、减肥、亲测成果等，不能自动写成“我”的经历。新增设想明确用“比如”等示例表达；外部研究不能变成个人体验。锁定开场若含未确认亲历，原话保留，列出需用户确认的具体事实，只将相关稿标为待核，其他稿继续完成；不把它升级成全批授权或事实审批。

除开场外，打散原结构后重建为自己的 2–3 个核心层次，加入原文没有的具体案例、动作或新判断。承接同一题材、观众、口语程度和开场逻辑；不要强行转到 AI，也不要逐条对应反驳或换词复述原作者步骤。观点转向要补桥，例如从债务压力转到副业选择，先说明副业如何承接本条的问题。

保留开场承诺了 3、4、10 项或其他数量时，正文必须完整兑现该数量；2–3 个核心层次可以作为上层组织，不能让承诺落空。篇幅由原题材和交付要求决定，不统一硬套长度。结尾收回本条问题，给出与正文相连的互动入口，并以指定创作者身份收口。

精确、易变的外部事实要用当前官方来源核验，记录真实可打开的来源。只检索会改变判断或需要支撑的内容；同主题可共享批次研究，不要求每篇 Deep Research。无法确认且影响发布的事实写成具体核对，不能编数字。

## JSONL 草稿接口

每行一个完整 JSON 对象，保存到 WORK 的 `drafts-*.jsonl`。所有字段根据该条作品填写，不用批量模板代替逐篇创作。

| 字段 | 内容 |
| --- | --- |
| `rank` | 准备阶段的主排序序号 |
| `video_id` | 该条唯一源视频 ID，按输入原样保留 |
| `opening_segment_count` | 连续锁定的开场 ASR 片段数，模型按语意选择 |
| `title` | 成稿标题 |
| `body` | 重建后的正文，不含锁定开场；渲染时脚本拼入原话 |
| `angle` | 本条新的表达角度或核心判断 |
| `new_example` | 新增具体案例；设想案例明确写“比如” |
| `shooting` | 1–3 条必要拍摄或配图建议 |
| `first_comment` | 与内容相连的首评，补充来源、案例或延伸观点 |
| `checks` | 0–3 条具体正文事实或发布核对，空数组不能当作已核实证明 |
| `sources` | 字符串列表，条目非空；精确外部事实记录当前官方链接，无外部事实用空数组 |
| `opening_checks` | 可选，0–3 条具体开头错字、亲历或听音疑点，默认 `[]` |

`opening_checks` 与 `checks` 不重复，每条 JSONL 中两者合计最多 3 个不同核对项目。超出时先完成可核验问题或合并同一事实项，不能静默截掉仍影响发布的问题；尚未解决的相关稿保留待核状态，其他稿继续。核对写明词句、事实、时间或需确认的动作，不写“注意合规”“检查一下”之类空话。开头字段缺省或没有疑点，仍标明尚未逐篇人工听音。

## 渲染、检查与续作

```bash
python3 "$SKILL_DIR/scripts/batch_rewrite.py" render \
  --work-dir "/absolute/path/rewrite-work" \
  --drafts "/absolute/path/rewrite-work/drafts-1.jsonl" "/absolute/path/rewrite-work/drafts-2.jsonl" "/absolute/path/rewrite-work/drafts-3.jsonl"
python3 "$SKILL_DIR/scripts/batch_rewrite.py" check \
  --work-dir "/absolute/path/rewrite-work"
```

完整模式默认要求所有源 ID 独一覆盖，rank 与原准备顺序一致。部分交付必须显式加 `--partial`，说明已完成、缺失和待核范围，不生成完整合集，也不能称全量改写完成。

续作时 `--drafts` 只传新增或本次修改的 JSONL，脚本按源 ID 合并已保存草稿；partial A 后只传新增 B 即可保留 A 并补全。同 ID 正文可更新，锁定开场的片段数和原话不能变；完整交付不能降级为 partial，手改成品保护仍生效。

脚本记录源快照和输出内容。源库漂移时报错，读取最新资料后建立新 WORK；不能改快照来掩盖变化。已经手改的成品拒绝静默覆盖，保留用户版本，需要重新渲染时使用新的独立工作与成品目录。恢复只推进已验证状态，不重做下载或 ASR。

即使源文件字节未变，工具升级若改变已冻结输入计划（helper plan），也会拒绝复用旧 WORK；保留旧 drafts，在新的 WORK/OUT 重新 prepare 后再渲染，不重做下载或 ASR，不篡改快照。

```text
WORK/
  run.json
  work-items.jsonl
  inputs-*.jsonl
  drafts-*.jsonl
  rendered-manifest.jsonl
  render-state.json
  quality-audit.json
OUT/
  001_赞…_评…_藏…_转…_标题-ID/
    01-洗稿口播稿.md
  00-按点赞排序索引.md
  00-综合参考排序.md
  00-排序说明.md
  00-开头核对清单.md
  00-N篇口播稿合集.md             仅完整渲染，N 为实际作品数
```

单篇成品含锁定开场与新正文、必要拍摄建议、首评、具体核对、来源与开头时间映射；索引和合集保留源 ID、本地来源链接、指标快照及辅助分说明。成品数量和命名由实际 N 决定。

## 交付验收

机械检查确认全部源 ID 独一覆盖、点赞顺序稳定、开场和时间映射可回读、Markdown 可打开、索引与实际 N 一致、部分和完整范围表述准确。`prepare`、`render` 和 `check` 每次复用源库离线验证并流式重算当前原片哈希，按实际报告中的 `source_hashes_verified` / `original_media_rehashed` 说明验证结果。`technical_complete=true` 仅用于完整技术交付；`semantic_review_certified=false` 与 `audio_review_certified=false` 不能通过结构检查变为已认证。`helper_llm_calls=0` / `helper_computer_use_calls=0` 与 `scope=deterministic_helper_only` 仅描述机械 helper，不包含创作与语义审核的模型用量。

另外逐篇从开场读到结尾，检查语意桥接、情绪连贯、开场承诺兑现、事实归属、新案例和自然收口。连续 28 字与源正文重合、跨稿 45 字重复可在 `quality-audit.json` 标为复核提示，锁定开场除外；检查否定句、夸张词、引用和题材背景后再判断，不能凭关键词或阈值自动判错。

修正实际语义问题后再交付，并分别报告完成范围、尚待用户确认的具体事实与尚未听音的开头。逐篇写作和语义审核仍消耗模型 Token，不能许诺“洗稿零 Token”，也不能以机械成功代替内容质量确认。
