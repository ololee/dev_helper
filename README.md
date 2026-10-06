# DevHelper 桌面助手

使用 Python 在 Mac 上运行同一套设备入口、笔记、记忆、Skills、向量、日程和资源管理。手机可以通过浏览器访问电脑，电脑也可以通过局域网调用已连接手机的 DevHelper。两端分别保存资料，选择设备决定这次查看哪一端；可开启资料同步，换电脑时从手机恢复。公开源码与私有资料分开保存。

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

## 共享剪贴板

支持在管理页面读取、编辑和发送**文本**剪贴板，单次上限为 **8192 字符**。可以从电脑复制到手机，也可以把手机文本复制到电脑。手机和电脑当前的剪贴板内容以实际读取结果为准。

自动共享默认关闭。需要时在界面中主动开启；开启后，助手会比较两端文字变化并同步。同步失败会显示原因。自动共享是运行期间的功能，关闭服务后停止同步。图片、文件和富文本剪贴板目前不在支持范围内。

## 本地资料、文件与日程

- Markdown 正文存放在 `data/knowledge/documents`，元数据、向量和日程记录存放在 `data/knowledge/knowledge.sqlite3`。
- 图片、视频与音频导入为 `data/knowledge/attachments` 中的独立副本，原文件保留。空间不设人为缓存配额，实际可用空间由磁盘决定。
- 默认浏览和共享范围是本目录的 `shared` 文件夹。可以把需要使用的文件放进去，或通过 `--shared-dir` 指定其他文件夹。目录浏览为只读，不提供删除原始电脑文件的功能；指向共享范围外的符号链接不会暴露外部文件。
- 本服务不在本地运行语言模型或向量模型。向量由连接客户端生成后导入，电脑本地保存并计算余弦相似度。正文修改或删除会使对应旧向量失效；语音识别可以另行配置可选的本地 Whisper 后端。
- 日程在桌面助手运行期间执行指定 MCP 工具。单次或重复任务均先持久化领取状态再执行；错过的重复次数跳过，已领取但中断的任务不会自动重放。
- Mac 端媒体裁剪、标注和录屏编辑尚未接入，界面隐藏或禁用这些能力；选择已连接手机可使用手机已有媒体工具。

## 笔记、录音与后台任务

管理页面的“笔记”入口也可直接访问 <http://127.0.0.1:8876/notes>。笔记使用 Markdown，默认不自动加载到记忆上下文。可以插入已有附件、从浏览器录音，或在手机端使用它的录音服务；录音停止后只保存音频，保存和资料同步均不触发转录、摘要或脚本。

需要转录时，在任务页面明确创建“录音转笔记”任务，选择音频以及是否生成要点。可以使用这台 Mac 的本地 Whisper，或配置兼容 OpenAI 音频转录接口的云端服务；要点整理和聊天使用配置的 DeepSeek API。未配置相应后端时任务会显示失败，不会自动安装模型或改用云端。摘要失败时已完成的转录仍保存在任务结果中。

转录追加到已有笔记时保留原 Markdown 和媒体引用，并检查笔记版本。并发修改不会被后台结果覆盖；手机音频处理会先保留原附件 UUID，再写入手机笔记和 Mac 副本。手机保存成功但 Mac 出现并发修改时，会显示部分完成并保留两端内容。

后台任务状态为排队、执行中、成功、失败或取消，保存在 `data/workflows/tasks.sqlite3`。服务重启后排队任务可继续，执行中被打断的任务会标记失败；工具和脚本不会自动重放。取消任务无法撤销已经完成的外部操作。

配置保存在本机 `data/workflows/config.json`，配置查询不返回 API 密钥。密钥、任务记录、本地模型和 ASR 虚拟环境均不进入公开源码或手机资料同步；换电脑后需要重新配置密钥与本地转录环境。使用云端转录和 DeepSeek 整理会将所选音频或文字发往配置的服务，只有明确提交对应任务才会调用。

脚本任务只运行配置目录中的 Python 或可执行文件，参数按数组传入；不把笔记内容或模型输出直接当作脚本执行。聊天默认返回工具计划，只向模型提供本次选中的工具；主动选择执行后才调用，并保留已完成的动作记录。

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

这个仓库可独立安装，不需要 Android 项目的其他目录。克隆你已确认的 GitHub 仓库，然后在仓库根目录建立上面的 Python 环境。仓库携带离线 Markdown 预览资源与原始许可证。

仓库内的 `skills/devhelper-deploy` 是可安装到 Codex 的部署 Skill。它可以从指定 GitHub 仓库安装或更新，再启动电脑版、发现手机并恢复资料。将该 Skill 目录复制到你的 Codex `skills` 目录，或让 Codex 使用这份 Skill 完成部署。部署脚本也可以直接运行；先替换示例中的仓库地址和目录：

```sh
python3 skills/devhelper-deploy/scripts/deploy.py install \
  --repo https://github.com/你的账号/已确认的仓库.git \
  --checkout "$HOME/DevHelper" --install-skills
```

安装流程先从手机下载笔记、记忆、Skills 及正文引用的附件，然后开启后续资料自动同步。记忆和 Skills 同步需要 Android DevHelper 1.8.1 或以上；笔记、音频和后台任务需要 1.9.0 或以上。手机不可达时仍可使用电脑本地功能，恢复流程会报告未连接，连接后可以重新运行 `restore`。

没有使用部署 Skill 时，新的空电脑首次连接手机会尝试下载恢复；手机暂不可达时继续等待连接，首次成功后不会再次把数据库当成新安装。不会把空电脑当成删除手机资料的命令。手动恢复可以使用以下接口：

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
python3 scripts/publish.py --configure https://github.com/你的账号/已确认的仓库.git --branch main
python3 scripts/publish.py --commit "说明这次源码更新"
python3 scripts/publish.py --push
```

远程 `origin` 必须与显式配置的地址一致，当前分支必须与配置分支一致。只提交 `publish-files.json` 中列出的代码、界面、测试和许可证；运行资料、日志、私人路径、其他未列明文件会阻止发布。推送前也检查可达提交历史，避免已删除的私人文件随历史上传。脚本不登录 GitHub、不创建仓库、不强制推送，也不建立后台自动推送。同步手机资料不会触发 GitHub 发布。

验证代码可以运行 `./.venv/bin/python -m unittest discover -s tests -v`。这些测试使用临时资料和合成文本，不读取系统剪贴板或真实手机资料。
