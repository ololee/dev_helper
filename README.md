# DevHelper 桌面助手

使用 Python 在 Mac 上运行同一套设备入口、笔记、记忆、Skills、向量、日程和资源管理。手机可以通过浏览器访问电脑，电脑也可以通过局域网调用已连接手机的 DevHelper。两端分别保存资料，选择设备决定这次查看哪一端；可开启资料同步，换电脑时从手机恢复。公开源码与私有资料分开保存。

## 2.13.0 本地与网盘直接互传

本地和百度网盘共用文件剪贴板：复制或剪切后切换目标位置粘贴，也可打开双栏直接批量拖拽文件和文件夹。无需另点上传或下载。任务显示文件与字节进度、当前文件、校验及源文件处理阶段，可取消并在刷新后继续查看后台结果。已完成的目标可直接打开。

跨端剪切先传完并校验整批目标，再清理源文件。本地到云端移动会回读云端文件核对 SHA256；下载比对已知归档 SHA256，未有归档指纹时记录实际字节 SHA256 并核验大小与云端版本。中断、冲突或部分失败保留未清理的源文件，明确报告每项结果。分类、备注和已有向量随可确认的同内容资源保留；云端剪切后的索引保存在独立云资源项目，原本地回收记录仍可恢复。详情见 [文件管理](ASSET_LIBRARY.md#文件管理)。

## 2.12.0 文件管理与代码预览

本地工程和百度网盘均支持复制、剪切、粘贴、重命名、删除和内部拖拽，文件与文件夹可批量处理。多选重命名使用 `{name}`、`{index}`、`{ext}` 模板，确认逐项目标后执行。Ctrl／Cmd+C、X、V、F2、Delete 对文件列表生效，代码查看器内仍按文本快捷键处理。移动与删除先显示计划，目标存在时明确提示，不覆盖或自动合并。本地删除进 DevHelper 回收站，可撤销或通过 MCP 恢复；网盘删除遵守百度的回收和保留策略。

本地移动与改名保留资源 ID、有效分类、备注和已有向量；复制为独立 ID 并复用同内容的分类和向量。Unity／Cocos 的同级 `.meta` 随素材一起处理，空目录也可浏览；文件中的相对引用文本不会被擅自重写。代码预览使用本地打包的 Monaco 0.52.2（VS Code 的编辑器核心），提供只读语法高亮、行号、折叠、查找和自动换行，无需外部 CDN。详见 [文件管理](ASSET_LIBRARY.md#文件管理)。

## 2.11.1 文件浏览与预览

资源库按项目、文件夹逐层浏览，单击选中、双击或 Enter 打开，右键提供打开、下载、复制路径和属性。支持 Ctrl／Cmd 切换选择、Shift 连选、方向键、Space、Esc，以及后退／前进／上一级、面包屑与地址栏。目录和百度网盘均采用文件夹优先的紧凑列表，并可切换缩略图。

图片、音视频、PDF 与文本使用全屏预览，音视频支持拖动进度条；HTML、SVG、脚本按纯文本阅读。云端未下载资源先显示下载入口，明确选择后只拉取这一项。Mac 本机页面可在 Finder 定位原件，或在关联应用中打开校验后的预览副本，原件保持不变。本机应用操作不对局域网或中转请求开放。详见 [文件浏览与预览](ASSET_LIBRARY.md#文件浏览与预览)。

## 2.11.0 云索引与按需资源

「云索引」可把当前工程或全部工程的分类、备注、OCR、依赖和现有 float32 向量作为标准 SQLite 版本同步百度网盘。分类上传完成后，新界面会另排相关工程的索引发布任务；原文件上传和索引发布分别查询完成结果。云版本放在 `catalogs/<snapshotUUID>/`，包含 `catalog.sqlite3` 和记录计数、文件 ID、大小及 SHA256 的小型 `manifest.json`。应用密钥、授权 token 和旧电脑工程根目录不随索引上传。

换电脑先授权网盘，再恢复选中索引版本：不重新扫描、分类或生成资源向量，合并时保留本机人工修改并报告冲突。恢复后可搜索分类、关键词和依赖，直接用已有向量查相似；新输入自然语言的语义搜索仍需同一向量模型 API 生成查询向量。原素材保持在云端，点击下载或预览时只拉取所选文件并校验 SHA256，不会自动铺满本机磁盘。手机原生「本机云资源库」可直接从已授权网盘恢复同一索引，电脑离线后仍可关键词和已有向量相似搜索；手机的查询 API Key 单独加密保存，不运行模型。电脑和手机 `/mcp/assets` 均有各自的索引版本、恢复、搜索与按需下载工具，电脑还支持索引发布。详见 [云索引与恢复](ASSET_LIBRARY.md#云索引换机恢复与按需原素材)。

## 2.10.1 百度网盘分类归档

本机游戏素材可先预览分类目录，再把原文件直接上传百度网盘。有效人工分类优先，其次是有效 AI 分类与文件类型；未知和低置信度项显示“需复核”，预览本身不联网、不调用模型。新界面按工程与主分类归档，多值标签保留在有版本的 JSON 清单中，不重复上传多份素材；旧客户端仍可使用原有任务快照布局。

本机分类管理使用标准 SQLite，元信息和备注为 JSON 字段，向量为 float32 BLOB。云端保存普通文件夹、二进制原文件及可读清单；2.11.0 另增加可恢复的 SQLite 云索引版本，旧归档清单本身仍不是向量备份协议。详见 [资源库分类与归档](ASSET_LIBRARY.md)。

## 2.10.0 自动向量与素材工作台

资源库可配置独立向量/分类 API，批量分类、自动生成向量、增量跳过、取消与重试，语义查询会自动生成查询向量。可选视觉模式使用二进制 JPEG 上传与 file_id，手机不运行模型。手机原生“游戏资源库”通过已配对电脑搜索同一索引，电脑需在线。

文件界面实际接入本地 Tabulator 6.5.0，加入缩略图网格、紧凑虚拟列表、服务器排序、多选和详情检查器，保留网盘及后台任务管理。配置与边界详见 [模型 API 与自动向量](ASSET_AI.md)。

## 2.9.0 百度网盘与游戏资源库

新增「资源库与网盘」入口：本机工程增量索引、文件指纹、图片颜色与快速相似、Unity/Cocos/Spine/DCEI依赖、独立备注、外部语义向量导入及显式云备份。电脑独立 HTTP MCP 为 `/mcp/assets`、`/mcp/netdisk`；手机原生百度网盘使用 `/mcp/netdisk`。文件上传下载保持流式二进制，MCP不内嵌base64素材。

网盘应用授权、目录权限与真实传输需填写自己的应用信息后验证。详见 [网盘配置](BAIDU_DRIVE.md) 和 [资源库能力与限制](ASSET_LIBRARY.md)。手机不运行任何模型；没有分析的OCR和无法确认的用途/依赖不会当作已识别结果。

## 2.8.5 AI 助手加载修复

资料管理内的 AI 助手允许加载同源的笔记工作区，修复浏览器因页面嵌入策略而显示空白的情况；外部页面和对象嵌入仍受限制。

## 2.8.4 游戏缓存关联与批量资料管理

小游戏按平台和已确认 AppID 汇总，详情同时显示原始包版本与热更新缓存，可合并导出。专用缓存元信息中明确的 AppID 可自动关联；缺少可靠身份的缓存可手动选择同平台游戏，关联来源会保留，支持解除。手动关联不虚构 AppID，不合并已知身份冲突的资源。旧记录 ID 和导出记录保持可访问。

资料支持任意文件流式上传，图片、视频、音频仍可预览，其他文件保留原始名称和内容供下载。支持多选、逐项反馈的批量删除，以及后台生成 ZIP 后下载；原件保留，被 Markdown 引用或正被读取的资源不删除。一次批量操作支持 1–100 条，资料容量仍由可用磁盘空间决定。临时批量 ZIP 随进程结束或最近任务清理失效，可重新生成。

## 2.8.3 原包与热更新缓存

手机的小游戏扫描覆盖宿主原包和可确认的下载资源目录：抖音的 `gamecaches`、`res` 与资源下载缓存，微信的 `MicroMsg/wxanewfiles` 中 `gamecaches`、`res`、`__GAME_FILE_CACHE`。原包、下载缓存分别显示数量和容量；缓存文件的原始字节和目录随 ZIP 传到电脑，无扩展名和 `.res` 的 Unity／团结资源也保留。

明确 AppID 且原包版本无歧义时关联缓存。微信哈希沙盒不能据此确认 AppID 或原包归属，按独立沙盒显示未关联缓存；同 AppID 多版本的资源保留为独立记录并提示版本未对应。编译缓存、临时文件、日志和存档不作为下载素材。未下载、已清理或只驻留内存的资源不在本机导出范围内，不自动访问游戏 CDN。

原包与缓存导入电脑后，可使用既有解包、素材导出和 AssetRipper 流程；资源依赖、团结格式或类型树不满足要求时报告实际失败，不承诺恢复完整游戏工程。

## 2.8.2 微信名称、图标与缓存时间

手机与电脑的小游戏列表支持按缓存修改时间最新／最早排序，未知时间排在末尾；详情分别显示缓存修改、扫描和导入时间。缓存修改时间不是最后游玩时间。

手机从小游戏专用元信息中按明确 AppID／包路径关联名称与图标。宿主专用信息库加密、关联有冲突或没有对应记录时保留未知并显示原因，不根据数字缓存文件名猜名称。图标只使用明确关联且经过格式验证的图片，包内按钮与引擎标志不作为应用图标。两端可手动补名称、选择图标，人工设置优先保留。手机导出原包 ZIP 后，关联资料、图标与缓存时间随包传到电脑。

列表接口支持 `GET games?sort=latest|earliest`；已关联的图标通过 `GET/HEAD games/{id}/icon` 读取，`POST games/{id}/icon` 接收 PNG、JPEG 或 WebP 原始图片，最多 2 MiB。手机与电脑图标保存为小尺寸静态 PNG。HTTP MCP 列表工具支持相同 `sort` 参数。私有游戏名称、图标、原包和元信息不发布到 GitHub。

## 2.8.1 微信缓存扫描修复

手机兼容新版微信的 `MicroMsg/appbrand/pkg/general`，读取明文 WXAPKG 的文件表和少量入口前缀，区分小游戏、小程序和待确认缓存。小游戏优先显示；仅有游戏分包时会提示主包及依赖可能缺失。没有可确认名称的包显示缓存文件名，数字文件名不充作 AppID。微信原包可传到电脑解包与导出素材，加密或损坏的包保留原文件并报告状态。

## 2.8.0 小游戏与 Unity／团结资源

工作台点击「小游戏」，或打开 `/minigames`。手机原生入口为「素材 → 小游戏管理」。列表来自本机缓存扫描和手动导入，支持抖音、微信的 AppID、名称与版本对应、手动补名称以及引擎证据；不是平台全量商店目录。哈希文件名不当作 AppID，未知名称和引擎会明确显示。H5 是部署方式，Wasm 文件本身不能确定 Unity。

手机按系统提供的宿主数据目录扫描小游戏缓存，只读取小游戏目录与元信息，原缓存保留。原包 ZIP 可手动保存或传给电脑。抖音 TPKG v2 解包器使用指定宿主版本的 RC4 密钥派生；版本或加密规则变化会明确报错，不宣称支持所有抖音版本。电脑同时支持明文 WXAPKG；微信加密包保留原包，不进行账号密钥绕过。Cocos 导出包含原包、解包文件、素材三种模式，保留相对目录和 cacheList 的 URL 映射，已有标准 Spine 骨架／图集／纹理可直接打开内置播放器。压缩纹理或 Cocos 自定义序列化数据不保证自动恢复成可播放 Spine。

Unity／团结完整导出在电脑运行。界面可配置已有 AssetRipper-CLI，或明确点击安装：固定上游源码版本，使用 .NET 10 构建当前 macOS/Windows 架构的执行程序；生成后为独立运行程序。可选下载私有 .NET SDK，团结转换依赖私有 Python 环境内的 UnityPy 1.25.3。不会把这些二进制、缓存、游戏资源或用户资料放进公开源码。上游源码、GPL 许可与第三方说明留在工具的私有安装目录。本机已实测 macOS ARM64；Windows 有运行架构与参数验证，尚未进行 Windows 设备实测。

团结识别依据含 `2022.3.*t*` 的实际版本与资源头；没有足够依据时显示「Unity／团结未区分」。已适配含类型树的团结 2022.3 资源，将对象按标准 Unity 类型树重新编码，保留 PathID 与对象引用，并规范包头；不是只替换版本字符串。无类型树、ACL 动画和微信流式纹理扩展会明确拒绝，不生成假成功。输出是重建项目，不能保证恢复原始源码、编辑器设置和全部依赖。

处理在后台任务执行，支持取消、阶段进度、ZIP/单文件下载和重启后的中断提示。导出与单独「DeepSeek 归纳」可明确选择发送素材清单进行分类整理；二进制资源保留本机。AI 失败仍保留已导出的素材，提示重试。任务完成通知沿用手机设置、响铃／震动和勿扰规则，离线或提醒被抑制时不声称已响铃。电脑页面显示任务进度和完成提示。

HTTP 入口为 `/api/knowledge/minigames`：`GET games`、`GET games/{id}`、`POST scan`、`POST import {attachmentId,platform?,name?}`、`POST export {gameId,mode,summarize?,notify?}`、`GET jobs/{id}`、`POST jobs/{id}/cancel`、`GET exports/{id}/archive`、`GET exports/{id}/files`、`POST exports/{id}/spine`、`POST exports/{id}/summarize`。选设备使用既有 `/device-api/{mac|android}` 路由，接口基于 HTTP，与 ADB 无关。电脑接收的是用户手动传送的游戏 ZIP；素材不会自动上传云端。

HTTP MCP 的 `devhelper_minigame_*` 提供列表、扫描、导入、导出、查询／取消任务、名称编辑、Spine、归纳及工具安装／配置。手机对应工具由 `devhelper_list_tools(device="android")` 读取。CLI 示例（在源码目录）：

```bash
python -m assetripper status
python -m assetripper setup --install-dotnet
python -m assetripper export --input /path/to/game --output /path/to/new-output --variant auto
python -m minigames --data /path/to/private-library import game.zip --platform douyin
python -m minigames --data /path/to/private-library export GAME_UUID --mode assets
```

Python 调用 `AssetRipperAdapter(private_root).export([Path(input)], Path(new_output), variant="auto")`，或 `MiniGameService(private_root).import_archive(Path(zip))`／`export({...})` 后查询 `job(id)`。不覆盖原件和已有非空输出目录；调用结束需 `MiniGameService.close()`。

## 2.7.0 Spine 播放器

在工作台点击「Spine」，或在当前服务地址后加 `/spine`。先选择「这台电脑」或已连接的手机，再导入 Spine ZIP 或包含骨架、图集与贴图的文件夹。资源按所选设备保存；通过电脑打开手机资源时，接口和贴图请求均指向所选手机。原 ZIP 作为附件保留，删除播放资源只删除已展开的资源包。

支持 JSON、SKEL 和 Unity 常见的 `.json.txt`、`.skel.bytes`、`.atlas.txt` 后缀，按骨架版本自动加载内置 3.8、4.0、4.1、4.2、4.3 运行库。播放画布支持移动、缩放和复位；控制区提供动画、皮肤、播放／暂停、循环、速度、播放头、PMA、调试线与多轨道控制。没有可识别版本时可手动选择；已知不支持的主次版本会明确报错。无需 CDN 或新增 Node 环境。

HTTP：先通过现有 `POST /api/knowledge/attachments/upload` 上传 ZIP，再用 `POST /api/knowledge/spine/import {attachmentId,name?}` 导入；列表 `GET /api/knowledge/spine/bundles`，详情／移除 `GET/DELETE /api/knowledge/spine/bundles/{id}`，资源 `GET/HEAD /api/knowledge/spine/bundles/{id}/files/{relativePath}`，支持 Range 下载。访问手机使用 `/device-api/{device}` 前缀，播放页为 `/device-ui/{device}/spine`。资源包内的脚本和 HTML 不会提供给播放器。

导入处理有单次资源包保护边界：ZIP 256 MiB、展开后 512 MiB、每条 128 MiB、10000 条目，仅支持普通 STORE／DEFLATE ZIP；不设置资源库总数量或总容量配额。第三方 Spine 运行库不适用本项目 MIT 许可，具体许可与来源记录随 `static/spine/runtime` 一起提供，见 NOTICE。

## 2.6.3 录音倒计时

点击开始录音后显示 3 → 2 → 1，每个数字 1 秒，结束后才真正开始录音。手机使用录音服务的统一倒计时，电脑浏览器在麦克风授权后倒计时，不会重复等待。倒计时可取消；离开录音页、切换设备或隐藏页面会取消尚未开始的录音，不生成空音频附件。录音计时从实际采集开始，已开始的手机录音可以继续后台录制。

HTTP/MCP 的手机录音开始接口立即返回准备／倒计时状态；按录音 UUID 取消只针对该次尚未开始的录音，迟到的取消不影响已开始或后续的新录音。录音仍只保存在所选设备，转写和发送保持手动。

## 2.6.2 全界面动效

手机和电脑的页面、导航、菜单、按钮、展开项和结果提示采用同一套短促的 ease-out 动效，依据 [animation-vocabulary](https://github.com/emilkowalski/skills/blob/main/skills/animation-vocabulary/SKILL.md) 的方向、连续性、按压反馈和减少动态效果原则安排。电脑导航选中背景连续移动，设备切换轻淡入；新增资料、录音和任务按稳定身份显示入场，后台刷新不重播。展开项可连续展开、收起或中途反向；手机首页、AI 回复、ima 卡片和悬浮按钮也有对应反馈。系统或浏览器关闭动画时恢复即时显示。媒体画布、裁剪框和播放头保持精确，关闭编辑器仍立即停止并释放播放器。

内嵌语音助手的成功 HTML 页面也加载本机动效资源，提供按钮、焦点和展开反馈；语音服务 API、音频流及模型运行方式保持原有行为。

## 2.6.1 交互与 AAC 波形修复

工作台、资料管理和笔记／录音／AI 页面共用轻量动效：页签按切换方向轻滑淡入，普通弹窗从触发位置展开，按钮提供按压与焦点反馈，状态、列表和展开项有短暂提示。关闭弹窗立即释放交互，退出视觉可随重新打开中断；全屏图片、视频和音频编辑保持原尺寸，播放头、ROI 和波形拖动不叠加位移动画。进度轮询不会反复闪动，系统／浏览器开启减少动态效果后取消进行中的动效。

AAC 解码器可能输出容器时长以外的尾部补齐样本。2.6.1 只把所选区间内样本计入波形，多余样本排空后忽略，避免报错或污染最后一桶。`decodedFrames` 表示有效区间样本帧数，`decodedPcmFrames` 表示实际解码帧数，`discardedPaddingFrames` 表示忽略的尾部帧数；跨声道绝对幅值与不归一化的规则保留。单次解码仍受区间工作量加 64 KiB 容差、最多 8 GiB 和处理超时保护，与录音存储配额无关。

## 一次接入其他工程

这台电脑已经运行 DevHelper 时，在其他工程使用同一个 HTTP MCP，不需要在每个工程部署一次服务。Python 3.11+ 执行：

```sh
python3 /你的DevHelper安装目录/scripts/import_project.py --project "/你的工程目录" --client both
```

默认接入 `http://127.0.0.1:8876/mcp`，同时添加 Codex 的工程 MCP 配置、Claude Code 的 HTTP MCP 配置和两端的公共 `devhelper-connect` Skill。既有其他 MCP 和设置保留不变；同名配置指向不同服务、非法配置或已手动修改的托管 Skill 会报告冲突，先检查全部目标再写入。重复导入相同配置不会重复添加。仅需一种客户端时使用 `--client codex` 或 `--client claude`。

自定义地址用 `--url "http://当前可访问地址:端口/mcp"`。建议连接电脑版入口，由电脑发现手机的最新地址或使用已配对中转。localhost 始终指运行客户端的那台机器；在另一台电脑、SSH 或云环境不能用它连接这台 Mac，需先部署当地服务或使用该环境可访问的指定地址。中转服务器地址不是可直接替代的 MCP 地址。

`--dry-run` 只检查连接和导入计划，不写文件；`--skip-check` 用于明确准备离线配置，仅生成配置且不声称连通；`--without-skill` 只接入 MCP。导入器只执行健康检查、MCP 初始化和工具目录读取，不调用资料读取工具、不保存或输出初始化上下文、不启动任务。返回的 `verification.status=verified` 表示脚本刚才验证了服务，并不表示已经运行的客户端会话自动获得了新工具。

在目标工程重新打开 Codex 或 Claude Code，完成客户端的正常工程信任或 MCP 批准；Claude Code 可用 `/mcp` 检查连接，Skill 可用 `/devhelper-connect`，Codex 可用 `$devhelper-connect`。工程里只有 HTTP 地址和公共连接 Skill，记忆、私有 Skills 正文、录音、向量和密钥仍通过 DevHelper 管理，不复制进工程或 Git。客户端现有本地配置或组织策略可能影响加载，请检查实际连接；导入器不会修改这些配置或批准设置。

配置格式遵循 [Codex MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)、[Codex Skills 文档](https://learn.chatgpt.com/docs/build-skills) 和 [Claude Code MCP 文档](https://code.claude.com/docs/en/mcp)。

## 通过手机生成视频帧拼图（手机 2.5.0）

手机打开「素材库 → 视频更多菜单 → 生成帧拼图」，也可在视频编辑页切换「帧拼图」模式。复用视频的画框 ROI 和时间选择，把真实连续帧或按间隔选出的帧合成带时间标签的新图片。手机上的 FFmpeg 解码、裁剪和缩放，libjpeg-turbo/libpng 编码图片，不调用任何模型。生成的图片可按已有附件同步方式传给电脑、插入笔记或交给视觉 AI。

电脑版 HTTP MCP 先调用 `devhelper_list_tools {"device":"android"}` 获取手机最新工具，再用 `devhelper_call_tool` 指定 `device=android`、`name=knowledge_start_video_storyboard`，将拼图参数放在 `arguments` 中。参数包括附件 `id`、`startSeconds`、`endSeconds`、`crop={x,y,width,height}`（旋转校正后的原视频像素）、`samplingMode=interval|consecutive`、`frameCount`、`intervalSeconds`、`columns` 与 `cellWidth`。默认从 0 秒开始、截止视频末尾，按 1 秒间隔最多取 12 帧，每行 4 格、单格宽 320 像素；默认 JPEG 质量 85，显示时间标签。间隔模式取每个非空时间段的第一张真实帧，连续模式取相邻解码帧；结束时间不包含在内，不足或空缺帧不会复制填充。录屏 `artifactId` 需先用 `knowledge_import_media_artifact` 转成手机附件 id，原视频保留。

通过同一电脑网关轮询 `knowledge_video_storyboard_status`，参数为 `{"jobId":"返回的任务 id"}`，确认 `state=completed` 后再传 `includeImage:true`。电脑网关会保留标准 MCP 图片内容，供支持图片的客户端读取；图片预览最长边最多 2048 像素、JPEG 最多 384 KiB，`imageRendering` 标明实际预览尺寸和缩放关系。完整图片仍在 `attachment.contentPath` 里，通过当前设备路由下载；`frames` 提供真实时间和每格图片坐标。状态默认只含元数据，不重复嵌入图片字节。`knowledge_cancel_video_storyboard` 可取消未完成的拼图任务。直接连接手机 MCP 时使用同名工具。手机需更新到 2.5.0；电脑网关动态读取工具目录，无需固定写入手机 IP。

HTTP 对应 `POST /api/knowledge/media/video-storyboard/start`、`status`、`cancel`；通过电脑版访问手机时使用 `/device-api/android` 路由前缀。HTTP 状态只接受 `jobId` 并返回元数据，图片从附件地址流式下载；`includeImage` 是 MCP 状态工具的可选参数。任务状态保存在当前手机应用进程，重启后生成的图片仍在素材库中。

## 双端视频播放与精调（2.5.1）

在手机素材库的视频菜单选择「剪辑视频」或「生成帧拼图」；文档里的视频附件也可进入同一编辑页。电脑版先选「这台 Mac」或已连接手机，再从素材库打开视频编辑。全屏编辑页提供真实播放与暂停、独立播放头和片段回放：播放头用于定位画面，起点、终点用于选择导出范围，拖动播放头不会改动裁剪区间。「播放片段」从起点播放，到终点暂停。

时间轴可放大、缩小、显示全片和左右移动可见窗口；缩放只改变时间轴的视野，不改变已选区间。起止时间支持秒数或 `HH:MM:SS.mmm`，也能选中「起点／终点」后以 100ms 或 1s 步长点击加减，或取当前播放头位置。输入时间后先应用，再导出。手机拖动时间或按加减时有轻触反馈，遵循系统触感设置，自动播放不振动。100ms 是调整步长；播放器 seek、源帧和编码器会影响实际画面与切点，不承诺逐帧或任意毫秒精度。

这台 Mac 可以本地导出视频的时间剪辑、画面裁剪和画笔、直线、箭头、框选、文字标注。使用安装依赖提供的 FFmpeg 和 Pillow，画面编辑输出 H.264 MP4，保留音轨并编码为 AAC；原附件保留。ROI 和标注使用旋转校正后的原视频像素，导出宽高向下调整为偶数，返回 `requestedCrop` 与 `actualCrop`。不运行语言模型，也不把视频自动上传云端。电脑本机图片编辑仍未接入；手机图片编辑能力按手机工具目录提供。

本地 MCP 工具为 `knowledge_start_video_attachment_edit`、`knowledge_video_attachment_edit_status`、`knowledge_cancel_video_attachment_edit`。先读取附件媒体信息，开始参数为 `{id,startSeconds,endSeconds,crop?,operations?,bitrate?,frameRate?}`；`id` 必须是所选设备已拥有的视频附件。`startSeconds`、`endSeconds` 必填，区间至少 0.1 秒；默认码率 4000000、输出帧率 30。启动返回 `jobId`，状态与取消只传 `{jobId}`，轮询到 `state=completed` 后读取新 `attachment` 和 `actualDurationSeconds`。导出时源附件受删除保护，失败或取消清理未发布副本，已经提交的新副本保留。任务历史最多记录当前进程的 32 个任务，服务重启后不能续跑，但已生成附件保留。

HTTP 对应 `POST /api/knowledge/media/video-edit/start`、`status`、`cancel`。电脑版处理本机附件可直接访问上述地址，访问手机加 `/device-api/android` 前缀；`/device-api/mac` 明确选择本机。MCP 也可通过 `devhelper_call_tool` 选择 `device=mac` 或 `android`，使用所选设备实际提供的同名工具。`knowledge_trim_video_attachment` 和 `POST /api/knowledge/media/trim-video` 只做 MP4/MOV 时间快速剪辑，参数为 `{id,startSeconds,endSeconds}`，流复制保留音轨并直接返回新附件；切点受关键帧影响，`precise=false`，需要核对实际时长。

## 双端音频播放、波形与剪辑（2.6.0）

手机从素材库或录音列表打开音频剪辑；电脑版先选择「这台 Mac」或已连接手机，再在素材库或录音列表打开「剪辑音频」。全屏页面先显示整段录音的真实波形，支持播放、暂停、独立播放头、试听选区、拖动边界以及缩放和平移。放大后的波形由所选设备重新解码该时间窗口，不把整段的粗略包络直接放大充当细节。起止时间支持秒数或 `HH:MM:SS.mmm`，播放头和区间边界可按 100ms／1s 加减；手机轻触反馈遵循系统设置。播放头定位不改变导出区间。

波形来自流式 PCM 解码：每个时间桶取所有声道样本的最大绝对振幅，数值为 0 到 1，不做音量归一化，也不将立体声混为单声道，避免反相信号相消。默认计算整段、1024 个桶，可请求 64 到 4096 个桶，空桶为 0。它显示振幅随时间的变化，与播放时显示频率分布的 FFT 频谱不同。手机用 OpenGL ES 2 Shader 绘制，播放频谱通过绑定当前播放器会话的 Android Visualizer 取得；系统要求 `RECORD_AUDIO` 权限，界面提供「授权播放频谱」，不会打开麦克风。未授权或设备不支持频谱时，仍可播放、查看波形和剪辑。电脑版浏览器使用 Web Audio 的实际 FFT 与 WebGL Shader；无 WebGL 时改用兼容画布，无 Web Audio 时保留波形和编辑，不显示虚构频谱。

先用 `knowledge_get_attachment_media_info {id}` 查看音频时长、`sampleRate`、`channels`、`waveformSupported` 与 `audioEditingSupported`。附件必须已存在于此次选择的设备；传输或相同 UUID 不能代替实际文件检查。四个音频 MCP 工具如下，直接连接设备时使用同名工具；经过电脑入口时先调用 `devhelper_list_tools {"device":"mac"}` 或 `{"device":"android"}` 获取实际 schema，再用 `devhelper_call_tool {device,name,arguments}` 指定设备。

| 工具 | 参数与结果 |
| --- | --- |
| `knowledge_get_audio_waveform` | `{id,buckets?,startSeconds?,endSeconds?}`；默认起点 0、终点为整段时长、1024 桶。返回 `peaks`、实际范围、采样率、声道数与 `decodedFrames`；`waveformType=absolute_peak_envelope`、`normalized=false`。缩放时传入窗口范围重新取样。 |
| `knowledge_start_audio_attachment_edit` | `{id,startSeconds,endSeconds,name?}`；起止时间必填、区间至少 0.1 秒，返回异步 `jobId`。 |
| `knowledge_audio_attachment_edit_status` | `{jobId}`；轮询进度，只有 `state=completed` 才读取新 `attachment` 与 `actualDurationSeconds`。 |
| `knowledge_cancel_audio_attachment_edit` | `{jobId}`；请求取消后继续查询最终状态，已经完成的新副本保留。 |

剪辑由所选设备的 FFmpeg 将指定区间重新编码为 AAC/M4A 新附件，保留源采样率和声道数，原录音保留，笔记引用不会自动改写。仅支持单音轨；非 AAC 支持的采样率会明确拒绝，不悄悄降采样。切点按解码样本处理，AAC 和容器边界仍可能影响实际播放时长，100ms 输入步长不代表任意毫秒精度。保存剪辑不会启动转录、摘要、模型或上传。

HTTP 对应 `POST /api/knowledge/media/audio-waveform`、`/api/knowledge/media/audio-edit/start`、`status`、`cancel`，请求正文与上述工具相同；媒体信息使用 `POST /api/knowledge/media/info {id}`。电脑访问手机时加 `/device-api/android` 前缀，明确本机可用 `/device-api/mac`。音频任务历史最多保留当前进程的 32 条，重启不恢复任务；已保存的附件继续保留。每台设备同时处理一个音频导出和一个波形请求。处理时保护源附件，取消清理未发布副本。源文件最长 24 小时、1–8 声道；波形单次请求限制解码工作量和处理时间，过长时可分窗口读取，附件库仍不设存储配额。

## 手机任务完成提醒（2.4.0）

手机原生管理的“设备 → 任务提醒”提供总开关、声音、震动和下一步提示，默认关闭。页面显示通知权限、系统勿扰和通知频道状态，并有明确的测试按钮。系统勿扰开启时整个提醒都会跳过；不会使用 Root 绕过通知权限或系统静音。锁屏只展示通用完成提示，打开通知可查看任务与下一步。系统自身的声音、震动和频道设置仍然生效；“已发送”只表示 Android 接受了通知，不能证明用户已经听到声音。

手机本机的已成功任务会按该设置提醒。电脑在“AI 助手 → 设置”开启“电脑任务完成后提醒手机”后，成功的后台任务也会向当前连接的手机发送提醒。失败和取消任务不发送完成提示。提醒结果单独记录在任务的 `notification` 字段，手机离线或通知失败不会改变任务本身的结果。连接采用已有的动态局域网发现或已配对的中转；断线时跳过，响应未知时不重发。电脑发出的提醒二十秒后过期，避免重连后的旧提示；两端系统时钟应正确。

Codex、Claude 等 HTTP MCP 客户端可在用户开启手机提醒后调用 `devhelper_notification_status`，然后在确认任务完成时调用一次 `devhelper_notification_notify`。工具需要稳定且唯一的 `eventId`，可带 `title`、`message`、`nextStep` 和 `taskId`；同一事件使用同一个 ID，手机不会重复响。配置工具 `devhelper_notification_config` 接受可选 `settings` 对象，仅按用户明确要求修改提醒设置。它们在手机和电脑的 HTTP MCP 均可调用，电脑会转发给当前手机。外部客户端需要在完成流程或自己的 hook 中调用此工具；仅连接 MCP 不会自动监听其他应用的任务结束。

HTTP 对应 `GET/POST /api/workflows/notifications/config`、`GET /api/workflows/notifications/status`、`POST /api/workflows/notifications/notify`，支持已有的设备路由与中转。POST 配置直接传入开关对象，MCP 配置则包在 `settings` 中。通知结果为 `posted`、`suppressed`、`duplicate` 或连接失败/未知状态，不能把 HTTP 200 或排队状态当成已提醒。

## 工作台与媒体编辑（2.2.0）

电脑版采用固定侧栏和设备切换栏，资料、笔记与录音按列表管理。编辑资料在独立面板中完成，媒体编辑占据整个浏览器窗口，关闭后恢复原工作台。手机原生管理分为“笔记、素材、任务、设备”四区；新增、编辑资料和任务使用完整页面，删除等简短确认才使用弹窗。

选择已连接手机，在素材库打开图片或视频后点击“编辑”。手机原生图片和视频编辑使用全屏画布：直接拖出裁剪框、拖动角点调整，底部切换画笔、直线和框选，支持撤销和重置；视频同时使用底部双端时间滑块选取片段。点击“另存”保存新素材，原文件保留。截图、录像和导入媒体使用同一入口。电脑版查看手机素材时，同样提供全屏画框编辑和视频时间剪辑；2.5.1 也支持电脑本机的视频导出，具体接口见上文。

手机 2.2.1 的悬浮采集菜单使用小图标；录屏时仅保留小号计时和闪烁的红色停止图标，可拖到上下左右边缘。应用和悬浮菜单开始录屏前有总计 1.5 秒的 `3 → 2 → 1` 倒计时，期间可取消；结束后按真实结果提示保存或失败。手机系统仍可能限制悬浮窗在受保护界面的显示。

## 启动与停止

先按下面步骤准备 `.venv` 环境。双击本目录的 **启动助手.command**，会启动后台服务并打开管理页面；双击 **停止助手.command**，会请求关闭本脚本启动的服务。

电脑管理地址：<http://127.0.0.1:8876/>。手机访问电脑时，使用管理页面显示的当前局域网地址；地址在运行时读取，不写死 IP。两台设备需要能够互相访问。

也可以在终端中运行：

```sh
cd /你的安装路径/DevHelper
./.venv/bin/python control.py start
./.venv/bin/python control.py status
./.venv/bin/python control.py stop
```

更换端口或共享文件夹：

```sh
./.venv/bin/python control.py start --port 8877 --shared-dir /绝对路径/要共享的文件夹
```

先停止当前服务，再用新设置启动。指定端口后，打开对应端口的网页；双击启动脚本默认使用 8876。停止时无需重复端口，脚本会读取自身的进程记录。它同时核对程序路径、启动参数、进程启动时间与服务身份，不会根据端口杀掉其他程序。

后台日志保存在 `server.log`，进程记录保存在 `control-state.json`。这些运行文件不属于手机资料。

新电脑需要 Python 3.11 或以上。首次安装环境：

```sh
cd /你的安装路径/DevHelper
python3 -m venv .venv
./.venv/bin/python -m pip install -e .
./.venv/bin/python control.py start
```

## 连接手机

在页面的设备管理中发现手机，或填写手机 DevHelper 当前显示的 HTTP 地址，例如 `http://手机当前IP:8765`。手机网络变化后重新发现或更新地址；不要使用旧 IP。手机 DevHelper 的 HTTP 服务需要开启。手机不可达时，电脑上的记忆、Skills、向量、日程和已导入资源仍可管理。

手机的截屏、录屏、录音、触摸、应用启动与其他 Android 能力由现有手机服务执行。某些后台操作，包括后台读取 Android 剪贴板，可能需要手机已经允许 DevHelper 使用 Root；系统限制不会被伪装成成功。

## 跨网络中转（2.1.0）

电脑和手机可以分别向 Linux 中转服务器建立出站 HTTP 连接，无需给设备开放公网端口。中转默认关闭。首次连接，在手机的设备连接页填写 HTTP 或 HTTPS 中转地址（可带端口）并生成 **6 位一次性配对码**，电脑的“中转与传输”页面填写相同服务器地址和六位码，点击“请求配对”，再在手机确认这台电脑。配对码五分钟有效，批准后不可再次使用；请求已发送不代表已配对。电脑会保存内部连接配置、自动连接并选择确认它的手机，以后无需重复输入。重新提交或网络重试使用同一个请求与私密回执，服务重启后也能继续等待确认。有效时间按服务器返回的剩余时长显示，是否过期以服务器状态为准，不要求设备时钟完全一致。

内部仍使用配对空间的长期访问凭据。HTTPS 保护传输；HTTP 无需证书，也没有链路加密，凭据与数据以明文传输。此改进简化首次配对，并未提供端到端加密或每台设备的独立密钥。长期凭据和临时回执只保存在设备私有配置，不返回给浏览器或状态查询，不写入浏览器 localStorage、公开 Markdown 或 Git。原有已保存配置和 `/api/relay/config` 的兼容接口继续可用，无需重新配对。

选择中转设备后，“同一局域网”开启时先读取对端最新地址，验证内部连接凭据和设备身份，再尝试局域网。手机关闭此开关时始终通过中转。局域网不可达时使用中转；请求已发出却未收到执行结果时显示结果未知，不重复执行工具或脚本。可以查询原请求的执行结果，不能把排队或送达状态当成成功。

连接与刷新列表自动同步资料、录音和任务的元数据，例如标题、ID、版本、摘要和大小；不会因此传输正文、录音文件或剪贴板内容。在缓存列表中点击某一条资料或文件进行手动同步；电脑端已导入的文件也可选择后发送到手机。文件按流传输，保留附件 UUID 并校验 SHA256，没有人为设置的小媒体配额。临时中转文件在 24 小时后清理；它不是永久录音备份。目标暂时离线时任务保持排队，在线后继续；服务中断的执行不会自动重放。

后台资料自动同步在中转模式下只更新元数据。局域网后台同步跳过含附件的资料，录音和其他媒体必须手动同步。剪贴板的主动读取和发送使用所选通道；自动共享仍需明确开启。本机 `POST /api/relay/pair {"serverUrl":"用户地址","code":"手机显示的六位码","name":"电脑名称"}` 提交配对，`GET /api/relay/pair/请求ID` 查询脱敏状态。MCP 的 `devhelper_relay_pair` 与 `devhelper_relay_pair_status` 提供相同流程，其他 `devhelper_relay_*` 工具提供脱敏设置、设备目录、传输和结果查询；工作流可通过 `transport` 选择 `auto`、`lan` 或 `relay`，来自手机的音频只在明确提交处理任务后导入。

中转私有配置、配对回执与执行记录存放在 `data/relay`，不进入资料同步或公开源码。设备目录在对端离线时仍可查看，但离线目录不代表文件已下载。

## 部署 Linux 中转服务器

中转源码在 `relay` 中，可用 Python 3.11 以上运行。Linux 服务器不运行转录模型或工具，也不需要克隆仓库。使用已有本地源码中的 `scripts/deploy_relay.py`，它只通过 SSH 上传明确列出的服务源码和依赖清单。服务器必须已准备 Python 3.11；SSH 默认使用 22 端口，公网需要放行 80（证书验证）与 443（HTTPS）。先用测试证书验证部署，再申请正式证书：

```sh
python3 scripts/deploy_relay.py --ssh-host 用户@服务器地址 \
  --identity-file /你的私钥路径 --public-ip 服务器公网IP --agree-tos --staging
python3 scripts/deploy_relay.py --ssh-host 用户@服务器地址 \
  --identity-file /你的私钥路径 --public-ip 服务器公网IP --agree-tos
```

部署创建专用 systemd 服务和每 12 小时的证书续期检查。证书续期使用热加载保留正在传输的请求；后续仅更新服务源码可加 `--update-only`。测试证书不能用于日常客户端连接。服务器配置、连接码、临时文件、数据库、证书与私钥均留在运行环境；SSH 私钥只用于本机连接，不随服务上传。完整参数见脚本 `--help`，不要把实际私钥、连接码或部署配置提交到 Git。

使用明文 HTTP 时指定一个空闲端口，无需申请证书：

```sh
python3 scripts/deploy_relay.py --ssh-host 用户@服务器地址 \
  --identity-file /你的私钥路径 --public-ip 服务器公网IP --http-port 8080
```

HTTP 部署先检查端口占用，发生冲突时停止，不修改其他服务。迁移保留 `/var/lib/devhelper-relay` 中的配对数据，备份原服务配置并关闭旧证书续期任务。需要放行选定端口，并把两端保存的服务器地址改成相同的 `http://服务器IP:端口`；保留原内部配对凭据即可，无需重新配对。

如果选定端口在当前网络中不可达，而 443 的 HTTP 测试正常，可以保留后端端口，再用独立的 TCP 入口将 443 转发到后端。例如后端使用 8081 时，入口运行 `socat TCP4-LISTEN:443,bind=0.0.0.0,reuseaddr,fork,nodelay TCP4:127.0.0.1:8081,nodelay`，两端填写 `http://服务器IP:443`。入口需由独立 systemd 服务管理并启用开机启动；使用专用用户和绑定低端口所需的能力。这里仍是 HTTP，443 端口本身不会将连接变成 HTTPS。端口的可达性应通过实际请求确认，不能仅凭端口号判断原因。

## 共享剪贴板

支持在管理页面读取、编辑和发送**文本**剪贴板，单次上限为 **8192 字符**。可以从电脑复制到手机，也可以把手机文本复制到电脑。手机和电脑当前的剪贴板内容以实际读取结果为准。

自动共享默认关闭。需要时在界面中主动开启；开启后，助手会比较两端文字变化并同步。同步失败会显示原因。自动共享是运行期间的功能，关闭服务后停止同步。图片、文件和富文本剪贴板目前不在支持范围内。

## 本地资料、文件与日程

- Markdown 正文存放在 `data/knowledge/documents`，元数据、向量和日程记录存放在 `data/knowledge/knowledge.sqlite3`。
- 图片、视频与音频导入为 `data/knowledge/attachments` 中的独立副本，原文件保留。空间不设人为缓存配额，实际可用空间由磁盘决定。
- 默认浏览和共享范围是本目录的 `shared` 文件夹。可以把需要使用的文件放进去，或通过 `--shared-dir` 指定其他文件夹。目录浏览为只读，不提供删除原始电脑文件的功能；指向共享范围外的符号链接不会暴露外部文件。
- 本服务不在本地运行语言模型或向量模型。向量由连接客户端生成后导入，电脑本地保存并计算余弦相似度。正文修改或删除会使对应旧向量失效；语音识别可以另行配置可选的本地 Whisper 后端。
- 日程在桌面助手运行期间执行指定 MCP 工具。单次或重复任务均先持久化领取状态再执行；错过的重复次数跳过，已领取但中断的任务不会自动重放。
- Mac 端视频已支持本机裁剪、标注和时间剪辑，录屏复制或导入为视频附件后使用同一入口；音频支持真实波形和 AAC/M4A 时间剪辑新副本。电脑图片编辑尚未接入；选择已连接手机可使用手机已有图片工具。

## 笔记、录音与后台任务

管理页面的“笔记”入口也可直接访问 <http://127.0.0.1:8876/notes>。笔记使用 Markdown，默认不自动加载到记忆上下文。可以插入已有附件、从浏览器录音，或在手机端使用它的录音服务；录音停止后只保存音频，保存和资料同步均不触发转录、摘要或脚本。

需要转录时，在“录音”页选择一段已保存的音频，点击“转写文字”只生成文字，或点击“转写并提炼”同时生成要点。结果追加到当前已保存的笔记；没有当前已保存笔记时会新建笔记。可以使用这台 Mac 的本地 Whisper，或配置兼容 OpenAI 音频转录接口的云端服务；要点整理和聊天使用配置的 DeepSeek API。未配置相应后端时任务会显示失败，不会自动安装模型或改用云端。摘要失败时已完成的转录仍保存在任务结果中。

转录追加到已有笔记时保留原 Markdown 和媒体引用，并检查笔记版本。并发修改不会被后台结果覆盖；手机音频处理会先保留原附件 UUID，再写入手机笔记和 Mac 副本。手机保存成功但 Mac 出现并发修改时，会显示部分完成并保留两端内容。

后台任务状态为排队、执行中、成功、失败或取消，保存在 `data/workflows/tasks.sqlite3`。服务重启后排队任务可继续，执行中被打断的任务会标记失败；工具和脚本不会自动重放。取消任务无法撤销已经完成的外部操作。

配置保存在本机 `data/workflows/config.json`，配置查询不返回 API 密钥。密钥、任务记录、本地模型和 ASR 虚拟环境均不进入公开源码或手机资料同步；换电脑后需要重新配置密钥与本地转录环境。使用云端转录和 DeepSeek 整理会将所选音频或文字发往配置的服务，只有明确提交对应任务才会调用。

脚本任务只运行配置目录中的 Python 或可执行文件，参数按数组传入；不把笔记内容或模型输出直接当作脚本执行。聊天默认返回工具计划，只向模型提供本次选中的工具；主动选择执行后才调用，并保留已完成的动作记录。

## DeepSeek 助手

手机与电脑均可配置自己的 DeepSeek Key。手机有 Key 时直接调用云端，不需要电脑运行模型。默认模型为 `deepseek-flash`，支持文字、图片和工具调用；可以选择思考模式并限制回复长度、工具轮数和调用次数。Key 只保存于当前设备的私有配置，读取接口仅返回是否已配置，不随笔记、记忆、Skills 同步或发布。设置里的“测试连接”会主动发起一条短请求。

助手支持润色、摘要、提取记忆、编写 Skill、规划任务、解释资料、辅助代码和分析媒体。整理结果先作为草稿返回，由用户选择保存为笔记、记忆或 Skill。开启资料检索后，请求会带上启用且自动加载的记忆与 Skills，以及相关检索片段；也可选择具体文档。它沿用现有本地检索和向量存储，不把聊天模型当作 embedding 接口。

图片仅在选中后发送实际图片数据；选中视频时抽取少量画面用于分析，电脑安装依赖时包含 `imageio-ffmpeg` 提供的 FFmpeg，优先使用已有可执行文件。视频分析不能识别未转写的声音，也不上传完整视频。录音继续使用独立 ASR 后端，转写完成后再交给 DeepSeek 提炼。支持情况及未连接设备会在能力接口中明确显示。

可将本次允许的工具加入助手，包括笔记、记忆、Skills、文件、剪贴板、日程、媒体和连接手机的实际工具。默认只生成工具计划；开启执行后才根据工具参数校验结果调用，并显示逐项结果。受控脚本只接受已配置脚本目录里的文件。密钥设置、设备连接设置和递归调用助手不提供给模型。自动日程可调用 `devhelper_workflow_submit` 提交 `chat` 或 `assist` 任务，仍使用任务里明确指定的工具和执行开关。

HTTP 接口：`GET /api/workflows/capabilities` 查询能力与可选工具；`POST /api/workflows/test` 测试已保存的配置；`POST /api/workflows/chat` 同步等待助手结果；`POST /api/workflows/tasks` 提交持久化 `chat`、`assist` 等任务，再查询任务状态。以上同样支持 `/device-api/mac` 与 `/device-api/android` 路由。MCP 对应 `devhelper_workflow_capabilities`、`devhelper_workflow_test`、`devhelper_workflow_chat`、`devhelper_workflow_submit` 和 `devhelper_workflow_run_script`。

`assist` 的 `action` 为 `polish`、`summary`、`extract_memory`、`create_skill`、`plan_tasks`、`explain`、`code` 或 `media`，接受 `message`、`text` 或 `noteId`。聊天和辅助任务共用 `useKnowledge`、`contextDevice`、`documentIds`、`imageIds`、`videoIds`、`allowedTools` 和 `executeTools`；文档最多 8 项、图片最多 4 项、视频最多 2 项。工具选择形式为 `{"device":"mac","name":"knowledge_list_documents"}`，手机本地工具使用 `android`。任务提交代表已排队，只有终态 `succeeded` 才表示处理完成。

Apple Silicon Mac 可按需安装本地 MLX Whisper。先启动电脑版，再明确运行：

```sh
./.venv/bin/python scripts/setup_asr.py --configure http://127.0.0.1:8876
```

这个可选步骤会安装 `.asr-venv`、音频解码器，并下载公开的 `mlx-community/whisper-small-mlx` 模型到本地 `data/models/` 中按模型仓库区分的独立目录。实际路径以脚本返回的 `settings.localModel` 为准；用 `--model` 选择其他兼容模型，完整参数见 `--help`。运行后的转录只加载已存在的本地模型；本机识别不上传音频。云端识别和 whisper.cpp 可在任务设置中单独配置。部署、录音或同步不会自动运行这个安装步骤。

## HTTP MCP

桌面 MCP 地址为 <http://127.0.0.1:8876/mcp>。其他设备使用电脑当前局域网 IP 和同一端口。传输使用 HTTP，不依赖 stdio、数据线或 ADB。

MCP 提供电脑本地笔记、记忆、Skills、向量、资源、日程和后台任务工具，并可以调用连接手机的能力。客户端可以读取 `knowledge://bootstrap` 或 `research_context` 提示词加载记忆；保存的 Markdown 是参考资料，不能自行覆盖用户请求或自动执行工具。

服务按既有需求不配置鉴权，默认监听局域网。需要只允许这台电脑访问时，可用 `control.py start --host 127.0.0.1` 启动。

## 语音助手（可选）

语音页面通过 HTTP 接入独立的 TTS 服务，默认地址是本机 `http://127.0.0.1:8793`。这个公开源码包不包含 TTS 模型或 MLX 服务；换电脑后，资料、文件和剪贴板功能可以先使用，语音生成需要另行准备兼容的 TTS 服务。服务地址保存在本地 `data/preferences.json` 的 `ttsUrl`，不提交到公开仓库。

## 新电脑部署与私有资料恢复

这个仓库可独立安装，不需要 Android 项目的其他目录。默认部署源为 [ololee/dev_helper](https://github.com/ololee/dev_helper)，克隆地址是 `https://github.com/ololee/dev_helper.git`；用户指定其他仓库时使用用户给出的地址。克隆后在仓库根目录建立上面的 Python 环境。仓库携带离线 Markdown 预览资源与原始许可证。

仓库内的 `skills/devhelper-deploy` 是可安装到 Codex 的部署 Skill。它可以从默认或用户指定的 GitHub 仓库安装或更新，再启动电脑版、发现手机并恢复资料。将该 Skill 目录复制到你的 Codex `skills` 目录，或让 Codex 使用这份 Skill 完成部署。部署脚本也可以直接运行；以下示例使用默认仓库，需要其他仓库时替换 `--repo` 的值：

```sh
python3 skills/devhelper-deploy/scripts/deploy.py install \
  --repo https://github.com/ololee/dev_helper.git \
  --checkout "$HOME/DevHelper" --install-skills
```

安装流程先从手机下载笔记、记忆、Skills 及正文引用的附件，然后开启后续资料自动同步。记忆和 Skills 同步需要 Android DevHelper 1.8.1 或以上；笔记、音频和后台任务需要 1.9.0 或以上。手机不可达时仍可使用电脑本地功能，恢复流程会报告未连接，连接后可以重新运行 `restore`。

没有使用部署 Skill 时，新的空电脑首次通过局域网连接手机会尝试恢复没有附件的资料；含录音或其他附件的记录等待明确同步，中转连接只更新目录。手机暂不可达时继续等待连接，首次成功后不会再次把数据库当成新安装。不会把空电脑当成删除手机资料的命令。手动恢复可以使用以下接口：

```text
GET  /api/sync/status
POST /api/sync/run          {"direction":"download"}
POST /api/sync/config       {"autoSync":true}
POST /api/sync/materialize  {"installSkills":true}
```

默认资料自动同步关闭，部署 Skill 按恢复与互联请求开启。开启后，两端的记忆与 Skills 可以双向同步；已确认的删除也属于资料修改。两端同时修改同一条内容时保留冲突，不静默覆盖。在管理页面处理冲突，或调用 `POST /api/sync/resolve {"id":"文档ID","keep":"mac或android"}` 明确保留哪一端。

目前同步 Markdown 笔记、记忆、Skills、启用状态和正文引用的附件，包括录音；向量、日程、后台任务与未引用素材仍分别留在各设备。引用截图缓存的 `/artifacts/...` 临时地址需要先导入附件库再保存，才能作为资料附件同步。

安装私有 Skills 是显式操作：启用且自动加载的 Skill 会被写入 `$CODEX_HOME/skills`，未设置时使用 `~/.codex/skills`。每份使用 `devhelper-private-<文档ID>` 专用目录并记录文件摘要，不覆盖同名非托管文件或用户手动修改。后续同步只维护这些托管副本；已删除或禁用的无修改副本可被清理，用户修改和其他 Skills 保留。部署脚本只报告成功、清理和失败计数；失败时检查接口的 `errors` 了解原因，不把正文打印到部署日志。

公开仓库不包含上述私有资料。请保留手机资料或本地数据备份；公开源码更新不能作为记忆备份。

## 源码更新与发布

在已配置的 Git 仓库使用 `git pull --ff-only` 更新。保留 `data`、`shared`、`.venv` 和可选的 `.asr-venv`；工作区存在本地源码修改时先处理修改，不使用重置覆盖。运行中的助手需要用同一目录的 `control.py` 受控停止，再安装更新依赖并重启。

`scripts/publish.py` 默认只检查发布清单，既不提交也不上传。配置仓库后，可按一次源码更新的验证结果执行提交与推送：

```sh
python3 scripts/publish.py
python3 scripts/publish.py --configure https://github.com/ololee/dev_helper.git --branch main
python3 scripts/publish.py --commit "说明这次源码更新"
python3 scripts/publish.py --push
```

远程 `origin` 必须与显式配置的地址一致，当前分支必须与配置分支一致。只提交 `publish-files.json` 中列出的代码、界面、测试和许可证；运行资料、日志、私人路径、其他未列明文件会阻止发布。推送前也检查可达提交历史，避免已删除的私人文件随历史上传。脚本不登录 GitHub、不创建仓库、不强制推送，也不建立后台自动推送。同步手机资料不会触发 GitHub 发布。

验证代码可以运行 `./.venv/bin/python -m unittest discover -s tests -v`。这些测试使用临时资料、合成文本、音频和视频，不读取系统剪贴板或真实手机资料，也不安装、卸载或清空手机应用。手机仪器测试应使用 Android 项目的独立 `verification` 应用与专用测试设备；不要对正常 DevHelper 包运行 connected 仪器测试，测试安装器可能卸载被测应用并删除其私有资料。普通手机联调通过已有 HTTP/MCP 连接，只创建和清理本次专用测试文件。
