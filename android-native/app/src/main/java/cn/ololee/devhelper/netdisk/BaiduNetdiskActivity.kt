package cn.ololee.devhelper.netdisk

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.lifecycle.lifecycleScope
import cn.ololee.devhelper.knowledge.NativeDesignTheme
import cn.ololee.devhelper.knowledge.NativeDesignTokens
import cn.ololee.devhelper.knowledge.NativeCard
import com.google.gson.JsonArray
import com.google.gson.JsonObject
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File
import java.util.UUID

/** Native settings/browser/transfer controls. No WebView and no account secrets in saved state. */
class BaiduNetdiskActivity : ComponentActivity() {
    private val runtime by lazy { BaiduNetdiskRuntime.get(this) }
    private var status by mutableStateOf(JsonObject())
    private var files by mutableStateOf(JsonArray())
    private var jobs by mutableStateOf(JsonArray())
    private var directory by mutableStateOf("")
    private var notice by mutableStateOf("")
    private var busy by mutableStateOf(false)
    private var hasMore by mutableStateOf(false)
    private var pendingSave: Pair<String,String>? = null
    private val select = registerForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { uris ->
        if(uris.isNotEmpty()) runOperation {
            val local=uris.map {uri ->
                runCatching {contentResolver.takePersistableUriPermission(uri,Intent.FLAG_GRANT_READ_URI_PERMISSION)}
                // The SDK's Uri interface predates scoped storage; stage SAF selections as binary files.
                val name=contentResolver.query(uri,arrayOf(android.provider.OpenableColumns.DISPLAY_NAME),null,null,null)?.use {cursor->if(cursor.moveToFirst())cursor.getString(0) else null}?:"file"
                val safe=runCatching {BaiduNetdiskPolicy.fileName(name)}.getOrDefault("file")
                val target=File(filesDir,"netdisk/uploads/${UUID.randomUUID()}/$safe");target.parentFile!!.mkdirs()
                try {contentResolver.openInputStream(uri)?.use {input->target.outputStream().use {input.copyTo(it,65536)}}?:error("无法读取选择的文件")}catch(failed:Exception){target.delete();throw failed}
                Uri.fromFile(target)
            }
            val result=runtime.uploadUris(local,directory);notice="已加入 ${result.get("fileCount").asInt} 个上传任务";refreshData()
        }
    }
    private val save = registerForActivityResult(ActivityResultContracts.CreateDocument("application/octet-stream")) {uri->
        val selected=pendingSave;pendingSave=null
        if(uri!=null&&selected!=null)runOperation {val source=runtime.content(selected.first);contentResolver.openOutputStream(uri,"w")?.use {output->source.inputStream().use {it.copyTo(output,65536)}}?:error("无法写入所选位置");notice="文件已保存"}
    }
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {NativeDesignTheme {Screen()}}
        runOperation {status=runtime.status();directory=status.value("remoteRoot");if(status.configured())refreshData()}
    }
    private fun runOperation(block:suspend ()->Unit) {
        if(busy)return
        busy=true;lifecycleScope.launch {try {withContext(Dispatchers.IO){block()}}catch(failed:Exception){notice=failed.message?.take(300)?:"网盘操作失败"}finally{busy=false}}
    }
    private fun refreshData() {status=runtime.status();if(status.configured()){files=runtime.files(JsonObject().apply {addProperty("path",directory);addProperty("limit",100)}).let {hasMore=it.get("hasMore").asBoolean;it.getAsJsonArray("files")};jobs=runtime.jobs().getAsJsonArray("jobs")}}
    @Composable private fun Screen() {
        var settings by remember {mutableStateOf(true)}
        var appId by remember {mutableStateOf("")};var thirdId by remember {mutableStateOf("")};var from by remember {mutableStateOf("")}
        var root by remember {mutableStateOf("")};var clientId by remember {mutableStateOf("")};var redirect by remember {mutableStateOf("oob")}
        var access by remember {mutableStateOf("")};var refresh by remember {mutableStateOf("")};var folder by remember {mutableStateOf("")}
        LaunchedEffect(status.toString()) {appId=status.value("appId");thirdId=status.value("thirdId");from=status.value("from");root=status.value("remoteRoot");clientId=status.value("clientId");redirect=status.value("redirectUri","oob")}
        Surface(Modifier.fillMaxSize().windowInsetsPadding(WindowInsets.safeDrawing),color=NativeDesignTokens.Background) {
            Column(Modifier.fillMaxSize()) {
                Row(Modifier.fillMaxWidth().padding(12.dp),horizontalArrangement=Arrangement.SpaceBetween) {
                    TextButton(onClick={finish()}){Text("返回")};Text("百度网盘",style=MaterialTheme.typography.titleLarge)
                    TextButton(onClick={runOperation {refreshData()}},enabled=!busy&&status.configured()){Text("刷新")}
                }
                if(busy)LinearProgressIndicator(Modifier.fillMaxWidth())
                LazyColumn(Modifier.fillMaxSize(),contentPadding=PaddingValues(20.dp),verticalArrangement=Arrangement.spacedBy(14.dp)) {
                    item {OutlinedButton(onClick={startActivity(Intent(this@BaiduNetdiskActivity,NativeFileManagerActivity::class.java))},modifier=Modifier.fillMaxWidth()){Text("本地与网盘 · 批量复制、剪切、拖拽")}}
                    item {NativeCard {Text(status.value("message","正在读取配置"));if(notice.isNotBlank())Text(notice,style=MaterialTheme.typography.bodySmall);Text("文件以二进制传输；手机与电脑使用各自的百度授权。",color=NativeDesignTokens.Muted,style=MaterialTheme.typography.bodySmall)}}
                    item {TextButton(onClick={settings=!settings}){Text(if(settings)"收起连接设置" else "连接设置")}}
                    if(settings) item {NativeCard {
                        Text("应用与授权",style=MaterialTheme.typography.titleMedium)
                        Text("在百度网盘开放平台创建应用并开通 basic、netdisk 权限。Android 包名 cn.ololee.devhelper；按平台要求登记安装包签名。AppID 与 API Key 是不同字段。",style=MaterialTheme.typography.bodySmall,color=NativeDesignTokens.Muted)
                        Field("AppID（必填）",appId,{appId=it});Field("用户 UID / thirdId（必填）",thirdId,{thirdId=it});Field("来源标识 from（必填，以应用接入信息为准）",from,{from=it})
                        Field("资源根目录（必填，与应用授权范围一致）",root,{root=it},"例如 /apps/百度应用名称");Field("API Key / clientId（浏览器授权可选）",clientId,{clientId=it});Field("已登记的回调地址",redirect,{redirect=it})
                        SecretField(if(status.flag("hasAccessToken"))"access_token（已保存，留空保留）" else "access_token（必填）",access,{access=it})
                        SecretField(if(status.flag("hasRefreshToken"))"refresh_token（已保存，留空保留）" else "refresh_token（可选）",refresh,{refresh=it})
                        Text("Token 使用 Android Keystore 加密，页面不回显。Secret Key 应保留在电脑或可信服务端，用于授权码换 Token；不要写入 APK。SDK 不代办应用审核或配额开通。",style=MaterialTheme.typography.bodySmall,color=NativeDesignTokens.Muted)
                        Button(onClick={val patch=JsonObject().apply {addProperty("appId",appId);addProperty("thirdId",thirdId);addProperty("from",from);addProperty("remoteRoot",root);addProperty("clientId",clientId);addProperty("redirectUri",redirect);addProperty("accessToken",access);addProperty("refreshToken",refresh)};runOperation {status=runtime.saveConfig(patch);access="";refresh="";directory=status.value("remoteRoot");notice="配置已保存"}},enabled=!busy){Text("保存设置")}
                        TextButton(onClick={runOperation {val link=runtime.authorization().get("authorizationUrl").asString;withContext(Dispatchers.Main){startActivity(Intent(Intent.ACTION_VIEW,Uri.parse(link)))}}},enabled=!busy){Text("打开百度授权页面")}
                        TextButton(onClick={runOperation {status=runtime.saveConfig(JsonObject().apply {addProperty("clearTokens",true)});jobs=JsonArray();files=JsonArray();notice="本机 Token 已清除；云端文件保留"}},enabled=!busy&&status.flag("hasAccessToken")){Text("清除本机授权")}
                    }}
                    if(status.configured()) {
                        item {NativeCard {
                            Text("网盘资源",style=MaterialTheme.typography.titleMedium)
                            Field("当前目录",directory,{directory=it})
                            Row(horizontalArrangement=Arrangement.spacedBy(8.dp)) {
                                OutlinedButton(onClick={runOperation {refreshData()}},enabled=!busy){Text("打开目录")}
                                Button(onClick={select.launch(arrayOf("*/*"))},enabled=!busy){Text("选择文件上传")}
                            }
                            Field("新目录名称",folder,{folder=it})
                            TextButton(onClick={runOperation {val child=BaiduNetdiskPolicy.fileName(folder);runtime.mkdir(JsonObject().apply {addProperty("path",directory.trimEnd('/')+"/"+child)});folder="";refreshData()}},enabled=!busy&&folder.isNotBlank()){Text("创建目录")}
                        }}
                        items(files.map {it.asJsonObject},key={"file:"+it.value("fsId")}) {file->NativeCard {
                            Text(file.value("name"),style=MaterialTheme.typography.titleMedium)
                            Text(if(file.flag("isDirectory"))"文件夹" else "${file.value("bytes")} 字节 · ${file.value("md5")}",style=MaterialTheme.typography.bodySmall,color=NativeDesignTokens.Muted)
                            TextButton(onClick={if(file.flag("isDirectory")){directory=file.value("path");runOperation {refreshData()}} else runOperation {val result=runtime.download(JsonObject().apply {addProperty("fsId",file.value("fsId"))});notice="已创建下载任务 ${result.value("jobId")}";jobs=runtime.jobs().getAsJsonArray("jobs")}},enabled=!busy){Text(if(file.flag("isDirectory"))"打开" else "下载到手机")}
                        }}
                        if(hasMore)item {TextButton(onClick={runOperation {val result=runtime.files(JsonObject().apply {addProperty("path",directory);addProperty("offset",files.size());addProperty("limit",100)});val next=JsonArray();files.forEach {next.add(it)};result.getAsJsonArray("files").forEach {next.add(it)};files=next;hasMore=result.get("hasMore").asBoolean}},enabled=!busy){Text("加载更多")}}
                        item {Text("传输任务",style=MaterialTheme.typography.titleMedium)}
                        items(jobs.map {it.asJsonObject},key={it.value("jobId")}) {job->NativeCard {
                            Text(job.value("name",job.value("jobId")),style=MaterialTheme.typography.titleMedium)
                            Text("${job.value("kind")} · ${job.value("state")} · ${job.value("transferredBytes","0")} / ${job.value("bytes","0")} 字节",style=MaterialTheme.typography.bodySmall)
                            val completed=job.value("state")=="completed";val terminal=job.value("state") in listOf("completed","failed","cancelled")
                            Row(horizontalArrangement=Arrangement.spacedBy(6.dp)) {
                                if(!terminal)TextButton(onClick={runOperation {runtime.action(job.value("jobId"),if(job.value("state")=="paused")"resume" else "pause");jobs=runtime.jobs().getAsJsonArray("jobs")}},enabled=!busy){Text(if(job.value("state")=="paused")"继续" else "暂停")}
                                if(!terminal)TextButton(onClick={runOperation {runtime.action(job.value("jobId"),"cancel");jobs=runtime.jobs().getAsJsonArray("jobs")}},enabled=!busy){Text("取消")}
                                if(completed&&job.value("kind")=="download")TextButton(onClick={pendingSave=job.value("jobId") to job.value("name","file");save.launch(job.value("name","file"))},enabled=!busy){Text("另存到手机目录")}
                            }
                        }}
                        if(jobs.isEmpty)item {Text("暂无传输任务",color=NativeDesignTokens.Muted)}
                        item {Text("后台传输由百度 SDK 调度；如系统限制后台运行，可保持 DevHelper 后台服务开启。完成状态请点刷新查看。HTTP MCP：/mcp/netdisk",style=MaterialTheme.typography.bodySmall,color=NativeDesignTokens.Muted)}
                    }
                }
            }
        }
    }
    @Composable private fun Field(label:String,value:String,change:(String)->Unit,hint:String=""){OutlinedTextField(value,change,Modifier.fillMaxWidth(),label={Text(label)},placeholder={if(hint.isNotBlank())Text(hint)},singleLine=true)}
    @Composable private fun SecretField(label:String,value:String,change:(String)->Unit){OutlinedTextField(value,change,Modifier.fillMaxWidth(),label={Text(label)},singleLine=true,visualTransformation=PasswordVisualTransformation(),keyboardOptions=KeyboardOptions(keyboardType=KeyboardType.Password,autoCorrectEnabled=false))}
    private fun JsonObject.value(key:String,fallback:String=""):String=if(has(key)&&!get(key).isJsonNull)get(key).asString else fallback
    private fun JsonObject.flag(key:String):Boolean=has(key)&&!get(key).isJsonNull&&get(key).asBoolean
    private fun JsonObject.configured():Boolean=flag("configured")
}
