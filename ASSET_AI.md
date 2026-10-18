# 游戏资源的模型 API 与自动向量

在“资源库与网盘”右上角打开“模型 API”，分别配置向量模型和分类模型。手机原生“游戏资源库”可搜索已配对的在线电脑，也可从网盘恢复分类索引与已有向量到本机，随后不依赖电脑进行关键词和已有向量相似搜索。手机不运行任何模型；新的自然语言查询可选配置外部向量 API，Key 仅在手机私密填写并用 Android Keystore 加密保存。

## 配置

向量接口采用 OpenAI-compatible `/embeddings` 协议：填写服务商 API 根地址、模型名称、API Key，可选维数和每批数量（1–32）。支持 OpenAI、SiliconFlow、阿里云百炼及兼容服务；以平台控制台的实际地址、地区、模型和权限为准。部分平台可点“读取模型”查询当前账号的模型列表，列表不代表每个模型都支持向量。百炼工作空间地址需按控制台填写。

分类接口独立配置地址、模型和 Key，每批 1–16 条。“文本与资源信息”通过 `/chat/completions`，适用于 DeepSeek 等兼容聊天服务；只看索引事实、OCR、备注与依赖，不声称看过图片。DeepSeek 聊天接口不能当作这里的向量接口。

“图片视觉分类”要求同时支持 Files 和 Responses 的视觉模型接口：将压缩至最长 1024 像素、不超过 1 MiB 的 JPEG 预览通过 multipart 二进制上传，然后用 `input_image.file_id` 分类。不使用 base64，不开放本机图片公网地址。透明图合成灰底、动图仅首帧，模型结果保留这些限制。只清理本任务自己上传的临时文件 ID，并设置一小时自动过期。非图片仍按文字信息分类。

保存配置时，密码框留空保留已有 Key；明确勾选清除才删除。更换 API 地址时必须重填 Key 或清除，避免将旧 Key 发送到新服务。状态接口与 MCP 不返回密钥，配置与任务保存在本机私有资料目录；向量可随用户选择的云索引备份到百度网盘。连接测试仅发送生成的固定文字或图片，不读取用户素材。

## 批量生成与搜索

选中资源或选择整个工程，可以“生成向量”“AI 分类”或“分类并生成”。联合任务先分类再给描述生成向量，逐条保存并显示步骤数、成功、跳过、失败、取消；完成一部分不会误报全部完成。支持取消和按当前配置重试失败项。重启只标记中断，不自动重放 API 调用。

描述向量包含文件路径、格式、尺寸、颜色事实、OCR、结构、依赖与有效分类备注。它是对这些描述的语义编码，不是原生图像、声音或三维模型的多模态 embedding；视觉分类可以先给图片补充实际内容描述，再做文字向量。模型没有输入到的图片、音频和文件内容不会被标为已理解。

模型空间由服务地址、模型、维数、处理版本区分，不能混合比较不同空间。相同文件 SHA、描述输入和模型已处理过时会跳过；注解/OCR更新使本应用的描述向量失效，同样注解不触发无效重算。原始外部图像/音频向量保留。请求进行中源文件或输入变化时，旧响应不会覆盖新信息。

分类与向量保存在本机标准 SQLite 数据库：分类备注为 JSON 字段，向量为按模型隔离的 float32 BLOB。模型 API 只负责推理，不承担资料库保存。分类结果能用于百度网盘的分类归档：有效人工分类优先，其次为有效 AI 分类，再按已分析的文件类型兜底；路径用途猜测、未知和低置信度项保留“需复核”提示。归档预览不会自动启动模型任务，API 尚未配置时也能按文件类型整理上传。

归档上传原始二进制素材，以及记录分类、证据、哈希和云端回执的有版本 JSON 清单。每个素材只选一个主目录，多值标签保存在清单里。2.11.0 的分类上传界面还会在文件上传结束后另排云索引发布任务，将相关工程的分类、依赖与现有向量保存为标准 SQLite 版本；也可在「云索引」独立发布全部或当前工程。模型 Key 和应用授权不随索引上传。文件任务与索引任务分别报告状态，不能以排队代替完成。详见 [云索引与恢复](ASSET_LIBRARY.md#云索引换机恢复与按需原素材)。

恢复云索引后，关键词、依赖与已有向量相似搜索可立即使用，不下载原件、不重做资源向量，也不调用模型。新输入的自然语言语义搜索仍需同一向量模型 API 编码查询；查询 API 的 Key 在新设备本机填写。原件仅在用户选择下载或预览时按需拉取。

关键词搜索使用本地索引；语义搜索自动调用已配置的向量 API 编码查询，再比较对应空间。重复查询有五分钟有界缓存。最多比较十万条当前向量，结果明确返回候选数及是否截断；这是流式余弦 Top-K，当前不是 ANN。界面显示最相关的前 200 条，可按工程和类型过滤。

## HTTP / MCP

- `GET/POST /api/resource-library/ai/config`：脱敏状态与本机配置。
- `GET /api/resource-library/ai/status`，`GET /api/resource-library/ai/models?kind=embedding`。
- `POST /api/resource-library/ai/test {kind: embedding|classification}`。
- `POST /api/resource-library/ai/jobs {kind: embed|classify|both, projectId?, assetIds?, force?}`。
- `GET /api/resource-library/ai/jobs`，`GET /api/resource-library/ai/jobs/{id}?offset=0&limit=200`。
- `POST /api/resource-library/ai/jobs/{id}/cancel` 或 `/retry`。
- `POST /api/resource-library/search {query, mode: text|semantic, projectId?, kind?, limit?, offset?}`。
- `POST /api/resource-library/similar {assetId, mode: semantic, model}`：直接比较资源已保存向量，不请求 API。
- `/api/resource-library/catalog/*`：发布／恢复已有分类和向量及按需拉取原件，详见资源库文档。

电脑 `/mcp/assets` 提供 `asset_ai_status/generate/jobs/job/cancel/retry` 与支持 `mode` 的 `asset_search`；Key 仅通过本机设置填写。手机 `/mcp/assets` 同时提供已配对电脑访问，以及独立的本机云索引恢复、搜索、依赖、已有向量相似和按需下载工具；恢复与原件拉取为显式异步任务。手机查询 API Key 不通过 MCP 参数传入或返回。访问电脑的二进制缩略图走局域网优先的配对连接，跨网走已有中转；本机云资源原件通过已授权的百度网盘 SDK 按需下载。

## 文件界面

列表实际集成 MIT 授权的 [Tabulator 6.5.0](https://github.com/tabulator-tables/tabulator)，固定本地部署，保留许可证和完整性记录。支持缩略图网格、虚拟列表、全结果排序、多选、详情检查器以及任务进度；模型字段和文件信息都作为不受信任的数据展示，不执行返回的脚本或工具。
