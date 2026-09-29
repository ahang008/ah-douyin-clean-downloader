# 故障排查

## 输入不是目标官方链接

单条下载需要官方视频链接或完整分享口令；博主主页、名片与整号请求使用 [批量流程](creator-batch.md)。不要从单条视频链接推断整号已经采全，也不要使用中间站改写的链接。

## 单条下载失败

先执行 `python3 scripts/doctor.py --format text`，确认 Python 与 curl。网络需要代理时使用用户明确配置的 `--proxy` 或 `AH_DOUYIN_PROXY`，不修改系统代理，不安装未知证书。

作品删除、私密、限频或接口变化可能导致不可用。只保留经脱敏的字段结构用于排查，不要求用户提供密码或导出 Cookie。播放字段变化时检查 `play_addr_h264`、`play_addr` 与 `bit_rate`，不得把 `download_addr` 当作后备播放源。

缺少 ffprobe 时可做文件头检查，但无法验证准确时长、编码和音频流数量；补齐 ffprobe 后再作完整媒体验收。

## 本地模型或转写依赖缺失

MLX 转写需要 Apple Silicon Mac、macOS 15+、Python 3.12 与 ffmpeg/ffprobe，按对应流程运行 `bootstrap.sh`。整号采集再运行 `bootstrap-browser.sh`。用 `transcribe_local.py --doctor` 查看本地模型；缺少时显式 `--download-model`，不要把推理失败自动改成托管 ASR。

单条校对后清理失败，报告脚本返回的 `work_dir` 残留路径，不宣称只剩成品。批量识别失败则保留原片、已完成机器稿与证据，按缺失 ID 续跑，不使用单条清理命令。

## 专用 Edge 登录或采集超时

用已安装的官方 Edge 与本流程专用资料目录；在专用窗口由用户普通扫码并在手机确认。二维码失效先刷新；仅聊天授权、窗口出现或点击登录按钮不能证明已登录。

增大 `--browser-timeout` 可给普通登录更多时间。采集超时或受平台验证限制时保留既有目录和成果，查看 `logs/collect.log` 与 capture/attempt 的固定错误类别。无需读取日常 Edge profile、钥匙串材料或自动操作系统许可窗口。

## 目录不完整

`catalog_complete=false` 时检查首游标、连续衔接、作者匹配、登录过滤和真实终点。游标重复、断链、空响应或分页上限都不能凭主页计数补成 complete。新刷新失败不能借旧 complete 目录宣称刷新成功。

图片作品、不可访问作品或已删除内容按实际范围说明。`--limit` 是样本，样本文件齐全不等于全量。

## 网络指向 198.18.* 或连接中断

默认 `--dns-mode auto` 仅在官方域解析失败或出现假地址时启用本机回环 HTTPS CONNECT/DNS 通道；`off` 直连，`always` 强制通道。通道只放行必要官方域和 CDN，保留正常 TLS 校验。查看 hostname/status，只有实际必需的官方域才可更新白名单。下载器的 `--dns-mode` 只作用于本进程，不改变系统 DNS。

不要通过忽略证书、关闭沙箱、削弱密码存储或复用其他浏览器会话绕过错误；通道或页面可达也不能替代分页完整性。

## 下载、识别或合集未完成

查看 `pipeline-summary.json` 的两个全量标志、`missing_download_ids` 与 `missing_transcript_ids`，再检查对应的 `logs/download.log` 或 `logs/transcribe.log`。ASR 错误不应一律要求重新扫码。下载完成后的识别收尾仍需读取最终清单；失败或缺失项使本次运行保持未完成。

执行原命令续跑，校验并跳过已完成项。合集由实际已验证正文导出，样本或旧合集不能代替全量目录验收。低置信、重复、尾段对齐或专名问题仍需听原片核对，另存校对版，保留机器原稿。
