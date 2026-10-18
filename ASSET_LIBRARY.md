# 游戏资源库

资源库只读取用户明确注册的工程目录，或恢复用户选择的百度网盘云索引，不执行工程脚本、不修改源文件。扫描结果、云端索引和原文件分开存储。扫描默认跳过生成目录及符号链接；解析限制、OCR可用性、跳过项都能通过 `asset_capabilities` 查询。

## 文件浏览与预览

资源库默认显示已登记或恢复的项目，双击项目或文件夹逐层进入。目录根据 SQLite 索引中的相对路径构建，云端索引也能浏览，无需先扫描源目录或下载原素材；文件夹的数量表示递归文件数。搜索框可搜索全工程内容，目录浏览和关键词／语义搜索保持分开。

单击选中，双击或 Enter 打开，Ctrl／Cmd 切换选择，Shift 连选，Space 切换当前项，Esc 退出预览或清除选择。右键提供打开、下载、复制相对路径、属性，以及当前电脑支持的 Finder 定位／关联应用预览。后退、前进、返回上一级、路径面包屑和相对地址栏用于导航。不会提供没有实际实现的重命名、移动或删除操作。

全屏预览提供图片、音频、视频、PDF 和文本。音视频采用浏览器原生播放控件；预览二进制接口支持 GET／HEAD 与单段 HTTP Range，可拖动进度且不将整段内容转换成 Base64。文本最多显示前 1 MiB，并标明截断；HTML、SVG 和脚本始终作为纯文本显示，不执行。浏览器不支持的媒体编码可下载或用本机关联应用预览。原有 `/content` 仍作为附件下载。

云端未下载素材先显示「下载并打开」，用户明确选择后仅拉取这一项，校验完成才预览。Mac 本机页面可以在 Finder 定位原件；「在本机应用中预览」使用已校验 SHA256 的只读副本，保留原扩展名和原件。系统应用操作只接受真实回环客户端与本机同源 POST，不接受局域网、中转或任意路径参数；HTTP MCP 不提供系统应用启动工具。

HTTP：

- `POST /api/resource-library/browse {projectId?,path?,kind?,sort?,order?,limit?,offset?}`：只返回当前层文件夹和文件。MCP 为 `asset_browse`。
- `GET/HEAD /api/resource-library/assets/{assetId}/preview`：内联预览，响应包含明确的 MIME 与 `nosniff`，不支持的格式返回错误。
- `POST /api/resource-library/assets/{assetId}/open {}`：本机关联应用预览副本。
- `POST /api/resource-library/assets/{assetId}/reveal {}`：本机 Finder 定位。
- `GET/HEAD /api/netdisk/jobs/{jobId}/preview`：预览已经完成的单文件网盘下载，支持单段 Range，不触发新下载。

## 分类与存储方式

本机资源目录使用标准 SQLite 数据库 `assets.sqlite3`，不是私有素材包格式。工程、文件指纹、路径、依赖标识和引用保存在独立表中；测量信息、用途判断、人工备注和 AI 分类以 JSON 字段保存。`annotations` 按人工／AI 来源分别记录，并绑定当时的文件 SHA256；文件变化后旧分类会标记失效。

语义向量存放在同一数据库的 `embeddings` 表，按模型空间、维数和文件 SHA256 区分，数值采用 float32 二进制 BLOB。关键词检索使用 SQLite 全文索引。分类标签、依赖和向量是索引信息，原始图片、模型、音频等仍是普通文件，不会编码成 Base64 塞进数据库。

文件类型依据格式、扩展名和可解析结构判断；用途判断保留来源、证据和置信度。AI 分类只在用户配置模型并启动任务后产生。人工有效分类优先于有效 AI 分类，没有有效分类时使用文件类型／分析结果兜底；仅凭路径得出的用途猜测不作为确定分类，未知或低置信度项标记“需复核”。多个标签可同时保留，上传时只选一个主分类目录，不为每个标签重复上传文件。

## 已实现的分析

- 所有文件：相对路径、格式、容量、修改时间、MD5、SHA256；SQLite增量索引与全文检索。
- 图片：可解码状态、尺寸、透明通道、主题色、dHash及颜色直方图。超出安全像素限制的图片仍能建立文件索引，但标记未分析。
- 资源分类：图片、模型、着色器、字体、材质、动画、音乐音效、Spine、代码、场景、预制体等。扩展名、结构字段和路径判断均保留证据及置信度。
- 依赖：Unity `.meta` GUID与序列化引用、Cocos UUID、Spine atlas页面、glTF外部资源、shader include、DCEI XML资源地址。未解析和多义引用单列；不凭文件名猜成已确认关系。
- 专用结构：支持OBJ/glTF统计、shader属性、可读字体家族、Spine动画名与骨骼数、WAV时长等。无法解析的私有二进制格式需先解包，不声称恢复全部结构。
- OCR：用户明确选择后，macOS 使用系统 Vision 离线识别中英文；其他平台使用已安装的Tesseract。引擎不可用时返回 unavailable，文字为 null。不会把“没有分析”记成“没有文字”。
- 人工与AI备注单独保存并绑定文件SHA256；源文件变化后标记旧备注，保留测量事实。

## 相似度与向量

完全重复使用SHA256索引；图片快速相似用64位dHash和48维RGB直方图。结果描述候选范围、比较数与是否截断，这些视觉特征不能当成语义向量。

语义向量可在“模型 API”配置后直接批量生成，联合任务先分类再编码描述，详见 [自动向量与模型配置](ASSET_AI.md)。也可通过 `asset_import_embeddings` 分批导入，按模型名称与维度隔离并归一化。只有同模型空间进行余弦比较；原文件变化后旧向量失效。大库候选比较明确标注限制，当前不是无损全库ANN检索。手机不运行模型。音乐、模型、代码等文件先提供结构、依赖与文本索引；并未伪造音频/3D语义向量。

新版批量 AI 分类在用户启动任务后调用独立配置的分类 API，逐条保存带证据的 AI 备注；文本模式不声称看过图片。可选 Responses 视觉模式以二进制上传压缩预览，再使用 file_id 分析。原有 DeepSeek 草稿接口仍可用。原始图片、视频、模型均通过 HTTP 二进制地址传输。

## 备份

选择最多 100 条本机资源，先“预览分类归档”，再将原始文件直接上传百度网盘。预览只读取已有索引，不联网、不调用 AI，也不上传素材；未配置模型仍可按已有人工分类和文件类型归档。预览显示分类来源、需复核项、远程路径和容量。确认启动时提交对应 `planDigest`；分类、文件指纹或目标目录变化后必须重新预览，避免实际上传与所见计划不同。归档保留源文件。

新界面默认 `layout=classified`，云端目录为：

```text
<选择的网盘目录>/<项目安全名>_<完整项目UUID>/<稳定类别目录>/<assetId>_<安全文件名>
<选择的网盘目录>/_catalog/<jobId>/manifest.json
```

类别目录采用稳定标识加可读名称，例如 `gui-界面`、`model-texture-模型贴图`、`image-图片`。完整项目 UUID 和资源 ID 用于避免同名工程、大小写或名称截断造成覆盖。

网盘保存普通目录和原始二进制文件，可直接通过百度网盘浏览和下载；清单是带格式标识及 `formatVersion` 的 UTF-8 JSON，记录原始相对路径、哈希、分类、标签、证据及逐项云端回执。不是只有本应用才能读取的私有二进制格式。历史调用省略 `layout` 时保留 `snapshot` 布局：以任务 ID 建目录、文件平铺，方便旧客户端继续使用。

分类计划以预览时的元信息为准，未知或需复核项不会伪装成已由 AI 确认。批量任务逐项记录成功、失败和未执行，上传或清单失败不会误报整批完成。云端上传成功只表示已有云副本；下载完成后才有目标设备的本地副本。备份目录和平台权限以已配置应用为准。

2.11.0 的新界面为分类归档设置 `syncCatalog:true`：原文件上传完成后，另排一个相关工程的云索引发布任务。`catalogSyncJobId` 与分类上传任务分别查询；文件上传完成不代表云索引已经完成。旧 API 调用省略 `syncCatalog` 时默认 `false`，不会给旧客户端增加隐式发布。独立发布与恢复见下节。

HTTP 流程：

1. `POST /api/resource-library/backup-plan {assetIds,remoteDirectory,layout:"classified"}`，读取预览和 `planDigest`。
2. `POST /api/resource-library/backup {assetIds,remoteDirectory,layout:"classified",planDigest,syncCatalog:true}`，明确启动上传。
3. `GET /api/resource-library/backups/{jobId}`，检查逐项状态与清单结果。

MCP 对应 `asset_backup_plan`、`asset_backup_start`、`asset_backup_job`；`asset_backup_start` 的 `layout`、`planDigest` 和 `syncCatalog` 为兼容旧调用而保留可选，分类归档需提交预览摘要。显式指定 `layout:"snapshot"` 可继续使用旧布局。`syncCatalog` 只提交给启动接口，不属于归档预览参数。

## 云索引、换机恢复与按需原素材

在资源库右上角打开「云索引」，选择当前工程或全部工程发布版本。发布的是标准 SQLite 文件 `catalog.sqlite3`，其中有分类、人工／AI 备注、OCR、文件指纹、依赖标识、相似特征、现有 float32 向量和已确认的云端原文件位置；小型 `manifest.json` 记录版本、计数、文件 ID、大小及 SHA256。向量以二进制 BLOB 保存，不转换成 Base64 或巨大 JSON 数组。

```text
/apps/<应用目录名>/catalogs/<snapshotUUID>/catalog.sqlite3
/apps/<应用目录名>/catalogs/<snapshotUUID>/manifest.json
```

可自选 `catalogs` 以外的索引父目录。每次发布新版本，不直接复制正在写入的 SQLite WAL，不覆盖其他设备的数据库。清单与原始素材归档的 JSON 清单用途不同：前者指向可恢复的完整索引，后者记录一次文件上传。版本列表仅下载小清单，不下载所有 SQLite 文件或原素材；当前每次检查至多 20 个版本，更多版本会报告 `hasMore`，可分目录管理。

应用的模型 Key、百度授权 token、SecretKey、传输队列和旧电脑的工程根目录不写入便携索引。可包含不带凭据的向量模型地址、名称、维数和空间标识，便于换机后配置匹配的查询 API。资源的描述、备注与 OCR 属于用户索引内容，会随索引备份。

换电脑时先授权同一百度网盘应用，打开「云索引」并选择版本恢复。恢复前检查 SQLite 格式、结构、大小、计数和 SHA256，再在本机事务中合并数据：不扫描原工程，不调用分类或向量生成 API，也不下载原素材。已有人工内容优先保留；内容不同的同名资源保留为独立版本并报告冲突，旧云向量与本机备注不一致时跳过旧向量并报告数量。不会把旧电脑的路径绑定为新电脑的源文件。

恢复后可直接浏览、按关键词查询、查看依赖，以及用「已有向量查相似素材」比较已保存的向量；这些操作不调用模型。输入新的自然语言进行语义搜索时，仍需同一向量模型 API 生成查询向量，资料中的每条资源向量无需重算。无需下载原素材就可搜索索引。

列表区分「云端未下载」「已下载缓存」「本机可用」。云端资源不会为了填满缩略图列表而自动下载全部原件；在详情点击「下载预览」或「下载原素材」后，只拉取该文件并校验 SHA256，再存入本机缓存供预览或保存。未上传或没有匹配回执的素材显示不可拉取，不把索引恢复成功当作原文件已有副本。

当前单版本便携 SQLite 校验范围为 2 GiB、最多 100 万素材、向量最多 4096 维，超出时明确失败；这是单版本解析边界，不是素材总库容量配额。后台发布、恢复和拉取任务持久化记录真实状态，服务重启标记中断，不自动重复云端动作。

HTTP：

- `GET /api/resource-library/catalog/status`：本机工程、资源、向量与云原件覆盖计数。
- `GET /api/resource-library/catalog/snapshots?remoteDirectory=catalogs`：可恢复版本与逐项失败原因。
- `POST /api/resource-library/catalog/publish {projectIds?,remoteDirectory?}`：发布已有索引。
- `POST /api/resource-library/catalog/restore {manifestFsId}`：恢复选中版本。
- `POST /api/resource-library/catalog/fetch {assetId}`：仅下载所选原件。
- `GET /api/resource-library/catalog/jobs`、`GET /api/resource-library/catalog/jobs/{jobId}`：任务、结果与冲突。
- `POST /api/resource-library/similar {assetId,mode:"semantic",model}`：使用已保存向量查相似，不请求模型。

电脑 HTTP MCP 对应 `asset_catalog_status/snapshots/publish/restore/fetch/jobs/job`；已排队不是已完成，应查询返回的 `jobId`。

## HTTP MCP

电脑提供 `/mcp/assets` 与 `/mcp/netdisk` 两个独立工具目录，原 `/mcp` 也能发现相关工具。手机提供 `/mcp/netdisk`，与Root研究工具目录分开。MCP参数与响应均为JSON；二进制文件使用返回的HTTP content地址，避免将文件编码进JSON。

手机端使用Android原生页面配置应用、选择文件和管理传输；Mac端用资源库与网盘管理页。私有配置、token、索引、游戏资产与测试证明不随公共源码发布。

手机原生“游戏资源库”保留「已配对电脑」来源，也可切换「本机云资源库」：先在手机网盘页授权同一应用账号，再点「从网盘恢复」选择云索引。分类、依赖和已有向量恢复到手机本机 SQLite 后，关键词与已有向量相似搜索无需电脑在线；原素材仍在网盘，选中下载并校验后才有手机副本。手机不运行模型。新的自然语言语义查询需在手机「查询 API」为已恢复的相同向量空间单独填写 Key，密钥用 Android Keystore 加密保存，不从网盘或 Mac 自动带入。

手机 `/mcp/assets` 提供 `asset_catalog_status`、`asset_catalogs`、`asset_catalog_restore {snapshotId}`、`asset_catalog_job`、`asset_similar` 和 `asset_fetch`；`asset_projects/search/get/dependencies` 指定 `source:"local-cloud"` 时访问手机本机索引，指定已配对电脑时继续走局域网优先连接。手机恢复 API 采用 `snapshotId`，电脑恢复采用 `manifestFsId`，应按各自工具 schema 调用。

手机版选择云版本后会切换本机云索引版本，已有下载的原素材缓存保留；需要全部工程时选择完整索引版本。Mac 恢复采用合并方式。手机的本机云索引目前用于浏览与搜索，分类修改通过电脑或来源索引发布新版本。
