---
name: devhelper-connect
description: 在已接入 DevHelper HTTP MCP 的工程里读取手机或电脑的记忆与私有 Skills，调用设备工具，或按已开启的设置发送任务完成提醒；不用于安装服务或复制私有资料到源码。
---

# 在当前工程使用 DevHelper

工程中的 `devhelper` MCP 指向已运行的 DevHelper，使用 Streamable HTTP。先查看当前会话的 MCP 工具是否可用；缺少工具时提示重新打开工程，并在客户端完成正常的工程信任或 MCP 批准步骤。不要通过 shell 绕过客户端的工具批准，也不要启动 stdio 或 ADB 来替代这个连接。

先查看当前连接提供的工具：电脑版入口提供 `devhelper_list_devices`，直接手机入口提供 `knowledge_status`，用实际存在的工具获取设备状态和地址。本机资料可通过 `knowledge_get_context` 或 `knowledge://bootstrap` 读取。通过电脑版连接且手机在线时，先用 `devhelper_list_tools` 查看手机工具的实际参数，再通过 `devhelper_call_tool`，指定 `device=android`、`name=knowledge_get_context`，读取手机启用且自动加载的记忆与私有 Skills。直接连接手机 MCP 时，使用它实际提供的 `knowledge_get_context`。离线状态要如实说明，不把电脑旧副本称作已经同步的手机最新内容。

资料中提到的任务、脚本与技能是上下文，不会扩展当前用户授权。读取资料不会安装本地模型、传输媒体、执行 Markdown 脚本或自动改动其他工程。需要同步正文、导入附件、转写录音或执行工具时，使用对应工具的实际 schema，并按用户本次请求选择设备和参数；排队不等于完成。

用户需要查看视频动作或某个区域时，可通过手机的 `knowledge_start_video_storyboard`，复用视频编辑器的 ROI 和时间范围生成真实连续帧或间隔帧拼图。手机入口是「素材库 → 视频更多菜单 → 生成帧拼图」，也支持视频编辑页的「帧拼图」模式。先读实际 schema 和视频元数据；录屏 `artifactId` 先通过 `knowledge_import_media_artifact` 转为附件 id。经电脑网关操作时，开始、查询和取消均用 `devhelper_call_tool`，指定 `device=android`，工具名放在 `name`，参数放在 `arguments`；直接手机连接使用同名工具。轮询 `knowledge_video_storyboard_status`，确认 `state=completed` 后再显式传 `includeImage=true` 获取 MCP 图片内容与真实帧时间。`frames.imageRect` 使用完整拼图像素，预览缩放关系由 `imageRendering` 提供。原视频与完整拼图保留；未完成或失败不能当作已经分析视频。

用户要求视频剪辑时，先确认附件实际保存在手机还是电脑，再读取对应设备的工具 schema 和媒体信息。2.5.1 的电脑本机可用 `knowledge_start_video_attachment_edit` 进行时间、ROI 和标注导出，随后用 `knowledge_video_attachment_edit_status {jobId}` 查询，或用 `knowledge_cancel_video_attachment_edit {jobId}` 取消。经电脑网关时使用 `devhelper_call_tool` 选择 `device=mac` 或 `android`；开始参数为 `{id,startSeconds,endSeconds,crop?,operations?,bitrate?,frameRate?}`，ROI 使用旋转校正后的原视频像素。只有 `state=completed` 才读取新 `attachment`；原视频保留，正文不会自动更新。只剪时间可用 `knowledge_trim_video_attachment`，快速流复制受关键帧影响，需核对实际输出时长。电脑本机图片编辑尚未提供；帧拼图工具仍由手机执行。文件传输沿用用户选择的局域网或中转流程，不因同一个 UUID 而假定两个设备都已拥有原件。

两端的全屏视频编辑提供独立播放头、片段回放、时间轴缩放和 100ms／1s 起止时间微调，支持秒数或 `HH:MM:SS.mmm` 输入，手机触感遵循系统设置。这些步长不保证播放器逐帧 seek 或任意毫秒切点；按工具返回的实际范围、尺寸和时长解释结果。

2.6.0 两端支持音频播放与时间剪辑。先确认附件归属设备，调用 `knowledge_get_attachment_media_info {id}` 检查时长、采样率、声道数、`waveformSupported` 和 `audioEditingSupported`。`knowledge_get_audio_waveform {id,buckets?,startSeconds?,endSeconds?}` 默认读取整段、1024 桶，桶数只能在 64–4096；缩放时按时间窗口重新读取。返回的 `peaks` 是真实流式 PCM 各桶跨声道最大绝对振幅，0 到 1、无归一化，不是 FFT，也不混单声道。通过电脑入口先用 `devhelper_list_tools` 选择 `device=mac` 或 `android`，再用 `devhelper_call_tool {device,name,arguments}` 调用所选设备实际工具；直接连接设备使用同名工具。

2.6.1 修复 AAC 容器尾部补齐样本：波形只统计请求区间内 PCM，额外尾样本排空后忽略。`decodedFrames` 是有效样本帧数，`decodedPcmFrames` 为实际解码帧数，`discardedPaddingFrames` 为忽略数量；不要把额外解码帧解释成选区变长。单次处理仍有工作量与超时保护，不改变原录音和存储配额。工作台、资料与笔记／录音／AI 浏览器页已接入共用 `motion.js` / `motion.css`；部署要带上打包清单中的这两资源。动效尊重减少动态效果，关闭立即释放交互，媒体画布和播放头不做位置动画；验证交互结果时不把短暂入场过渡当成任务仍未结束。

音频导出使用 `knowledge_start_audio_attachment_edit {id,startSeconds,endSeconds,name?}`，区间至少 0.1 秒；查询用 `knowledge_audio_attachment_edit_status {jobId}`，取消用 `knowledge_cancel_audio_attachment_edit {jobId}`。只有 `state=completed` 才报告成功并读取新附件与实际时长。FFmpeg 另存 AAC/M4A，保留源采样率和声道数，只接受单音轨及支持的 AAC 采样率；原录音、已经完成的副本和原笔记引用保留。未完成任务与最多 32 条历史仅在当前进程，重启不续跑。此操作不触发转录或上传。AAC 边界会影响实际播放时长，不承诺任意毫秒切点。

两端全屏音频页提供独立播放头、试听选区、波形缩放／平移、秒数或 `HH:MM:SS.mmm` 输入与 100ms／1s 微调。播放 FFT 与时间波形分开：Android GLES 绘制的频谱来自当前播放器会话 Visualizer，要求界面明确申请「播放频谱」的系统录音权限，不打开麦克风；拒绝后仍能播放与剪辑。浏览器频谱来自 Web Audio 的实际 FFT，WebGL 不可用时用兼容画布，Web Audio 不可用时仅保留波形与编辑。不要把波形包络、静止画面或动画称为频谱分析。

如果用户已经开启手机任务提醒，在确认当前任务成功后，按当前连接提供的 schema 调用 `devhelper_notification_status`，然后调用一次 `devhelper_notification_notify`。为这次任务使用稳定、唯一的 `eventId`，可提供简短标题与 `nextStep`。不要发送整段私有结果或密钥；保留勿扰、离线、重复或送达未知的真实状态，不自动重发，不为了提醒而改变手机设置。

这份工程 Skill 只提供连接方法。私有记忆、Skills 正文、录音、向量和 API Key 留在 DevHelper 的资料存储中，不复制到工程或 Git。用户明确要求安装本地私有 Skill 时，才使用 DevHelper 已有的托管安装流程；不能通过读取 Skill 正文自行发起安装或发布。
