# Android 原生互传组件源码

本仓库主要分发 DevHelper 电脑版。`android-native/` 额外保存 **2.13.0 本地与百度网盘原生互传组件**及相关接线位置，方便审查、比较和集成到已有 DevHelper Android 工程。它不是完整、独立的 Android 工程；直接进入该目录不能构建完整 APK。

此快照包含原生文件管理页、二进制网盘客户端、批量传输引擎、后台服务、HTTP/MCP 工具、展示与剪切状态判定，以及本次相关的 JVM 和隔离设备测试。为说明集成位置，同时包含本次修改过的原生资源/管理/网盘页面、知识 HTTP 入口、应用 Gradle 文件和 Manifest。后两者是已有工程的配置参考，不表示其余构建依赖已经随包提供。

## 集成到已有工程

1. 使用自己的现有 DevHelper Android 工程，按快照中的原始相对路径合并代码；保留该工程其他源码和资源。原生界面还依赖现有的 `NativeDesign`、`NativeMotion`、管理状态、资源库及百度授权等组件，不能只复制本目录作为空工程运行。
2. 检查 Manifest 中的原生文件管理 Activity、`dataSync` 前台服务及对应权限，合并已有 HTTP 和 MCP 分发入口。复制、剪切与拖拽共用 `plan → execute → job` 流程；取消通过对应任务 ID 请求。保留后端校验及明确的源文件清理结果，不能仅根据进度达到 100% 就删除源文件。
3. 自行从合法渠道取得工程需要的百度 Android SDK。现有工程使用本地 `aPaasBase-oauth.aar`、`aPaasFile-oauth.aar`、`aPaasTransmit-oauth.aar`，位于 `app/libs/baidu/`。这些用户提供的 AAR 没有随公开快照发布。配置自己的百度应用、账号授权及获准访问的应用目录，不把 Token 或应用密钥写进源码。
4. 正常 debug/release 工程仍需自己的 Firebase/Google Services 配置、Android SDK/NDK/CMake 和既有原生媒体依赖。生命周期依赖版本参考见 `gradle/libs.versions.toml`。当前配置参考为 JDK 25、SDK 37、NDK `29.0.14206865`、CMake `3.31.6`、最低 API 29、ARM64。此快照不提供 `google-services.json`、libjpeg-turbo/libpng/FFmpeg 源码或编译产物，也不负责下载这些依赖。先按已有完整工程的构建说明准备它们，再构建 APK。
5. JVM 测试应在完整工程中运行。设备测试固定使用独立的 `verification` 变体和独立数据目录；该变体跳过 Google Services 处理，但仍需要百度 AAR 及其他构建依赖。不要把仪器测试指向日常使用的 `cn.ololee.devhelper` 包，或通过清除、卸载正常应用来测试。

手机本地目录通过系统文件夹选择器授予持久读写权限，默认使用应用自己的资源文件夹；实际传输由手机执行，不通过电脑代理。文件以二进制流传输，保留目录结构，并提供批量工作进度、当前文件、校验阶段和取消状态。剪切需先确认整批目标，再处理源；中断、未知回执和部分清理均应保留真实逐项结果。

## 公开内容边界

`scripts/android_native_sources.py` 逐项列出可发布路径，仅接受 `.kt`、`.java`、`.kts`、`.xml`、`.toml`，检查文件存在且路径不含符号链接。不递归收集 Android 工程，也不从构建目录收集文件。开发目录和已发布仓库采用同一份列表，使再次打包时仍能保留此组件快照。

公开快照不包含 AAR、APK、原生可执行文件、其他编译二进制、签名材料、Google 项目配置、访问令牌、用户文件、数据库、缓存、运行数据或测试录制素材。第三方 SDK 和完整工程其他组件沿用各自许可，不能将它们视为本快照已提供或已授权再分发的内容。
