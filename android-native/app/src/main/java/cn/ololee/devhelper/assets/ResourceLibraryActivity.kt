package cn.ololee.devhelper.assets

import android.content.Intent
import android.graphics.BitmapFactory
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.lifecycleScope
import cn.ololee.devhelper.knowledge.*
import com.google.gson.JsonArray
import com.google.gson.JsonObject
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.launch
import kotlinx.coroutines.delay
import androidx.compose.ui.text.input.PasswordVisualTransformation
import cn.ololee.devhelper.netdisk.BaiduNetdiskActivity
import cn.ololee.devhelper.netdisk.NativeFileManagerActivity
import kotlinx.coroutines.withContext
import java.util.Locale

/** Native resource browser with a computer-independent restored cloud catalog. */
class ResourceLibraryActivity: ComponentActivity() {
    private val client by lazy {ResourceLibraryClient.get(this)}
    private val cloud by lazy {CloudAssetCatalog.get(this)}
    private var cloudStatus by mutableStateOf(JsonObject())
    private var snapshots by mutableStateOf<List<JsonObject>?>(null)
    private var apiConfig by mutableStateOf<JsonObject?>(null)
    private var workMessage by mutableStateOf("")
    private var servers by mutableStateOf<List<JsonObject>>(emptyList())
    private var projects by mutableStateOf<List<JsonObject>>(emptyList())
    private var results by mutableStateOf<List<JsonObject>>(emptyList())
    private var serverId by mutableStateOf("local-cloud")
    private var projectId by mutableStateOf("")
    private var query by mutableStateOf("")
    private var mode by mutableStateOf("text")
    private var busy by mutableStateOf(false)
    private var notice by mutableStateOf("")
    private var total by mutableStateOf(0)
    private var hasSearched by mutableStateOf(false)
    private var detail by mutableStateOf<JsonObject?>(null)
    private var dependencies by mutableStateOf<JsonObject?>(null)
    private var preview by mutableStateOf<android.graphics.Bitmap?>(null)
    private var previewNotice by mutableStateOf("")
    private var transport by mutableStateOf("")
    private var resultSearchKey by mutableStateOf("")
    private var operationVersion=0L
    private var operationJob: Job?=null
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {NativeDesignTheme {Screen()}}
        refreshServers()
    }
    private fun operation(block: suspend ()->Unit) {
        if(busy)return
        val version=++operationVersion
        busy=true;notice=""
        operationJob=lifecycleScope.launch {try {block()} catch(stopped: CancellationException) {throw stopped} catch(failed: Exception) {workMessage="";notice=failed.message?.take(400) ?: "资源库暂不可用"} finally {if(version==operationVersion)busy=false}}
    }
    private fun closeDetail() {++operationVersion;operationJob?.cancel();operationJob=null;busy=false;detail=null;preview=null;dependencies=null;notice=""}
    private fun searchKey()=listOf(serverId,projectId,mode,query.trim()).joinToString("\u0000")
    private fun refreshServers()=operation {
        cloudStatus=withContext(Dispatchers.IO){cloud.status()}
        val value=withContext(Dispatchers.IO){runCatching {client.servers()}.getOrDefault(JsonObject())}
        servers=value.getAsJsonArray("items").objects()
        if(serverId!="local-cloud"&&servers.none {it.text("deviceId")==serverId})serverId="local-cloud"
        projects=withContext(Dispatchers.IO){if(serverId=="local-cloud")cloud.call("asset_projects",JsonObject()).getAsJsonArray("items").objects() else callTool("asset_projects",arguments()).getAsJsonArray("items").objects()}
    }
    private fun callTool(name:String,args:JsonObject)=ResourceLibraryTools.data(this,name,args)
    private fun arguments()=JsonObject().apply {if(serverId=="local-cloud")addProperty("source","local-cloud") else if(serverId.isNotBlank())addProperty("serverDeviceId",serverId)}
    private fun chooseServer(id:String)=operation {
        if(id!="local-cloud")withContext(Dispatchers.IO){client.selectServer(id)}
        serverId=id;projectId="";results=emptyList();hasSearched=false;total=0;detail=null;preview=null;transport=""
        projects=withContext(Dispatchers.IO){callTool("asset_projects",arguments()).getAsJsonArray("items").objects()}
    }
    private fun showSnapshots()=operation {snapshots=withContext(Dispatchers.IO){cloud.snapshots().getAsJsonArray("items").objects()}}
    private suspend fun awaitJob(initial:JsonObject):JsonObject {
        if(initial.text("state")=="completed")return initial.getAsJsonObject("result")?:initial
        val id=initial.text("jobId")
        while(true){val job=withContext(Dispatchers.IO){cloud.job(id)};workMessage=job.text("message","正在恢复云资源…");when(job.text("state")){"completed"->{workMessage="";return job.getAsJsonObject("result")?:job};"failed"->error(job.text("error","云资源操作失败"))};delay(650)}
    }
    private fun restoreSnapshot(id:String)=operation {
        snapshots=null
        val initial=withContext(Dispatchers.IO){cloud.restore(id)}
        cloudStatus=awaitJob(initial);serverId="local-cloud";projectId="";results=emptyList();hasSearched=false;total=0
        projects=withContext(Dispatchers.IO){cloud.call("asset_projects",JsonObject()).getAsJsonArray("items").objects()}
        workMessage="分类与向量已恢复；原素材按需下载"
    }
    private fun showApiConfig()=operation {apiConfig=withContext(Dispatchers.IO){cloud.config()}}
    private fun saveApiConfig(space:String,key:String,clear:Boolean)=operation {
        withContext(Dispatchers.IO){cloud.saveConfig(JsonObject().apply {addProperty("space",space);addProperty("apiKey",key);addProperty("clearKey",clear)})}
        apiConfig=null;cloudStatus=withContext(Dispatchers.IO){cloud.status()};workMessage="查询模型配置已保存在手机，密钥不会同步到网盘"
    }
    private fun downloadAsset(asset:JsonObject)=operation {
        val initial=withContext(Dispatchers.IO){cloud.fetch(asset.text("id"))};val result=awaitJob(initial);detail=result
        loadPreview(result)
    }
    private fun findSimilar(asset:JsonObject)=operation {
        val value=withContext(Dispatchers.IO){cloud.call("asset_similar",JsonObject().apply {addProperty("assetId",asset.text("id"));addProperty("limit",50)})}
        results=value.getAsJsonArray("items").objects();total=results.size;detail=null;preview=null;hasSearched=true;resultSearchKey=searchKey();workMessage="按已有向量排序，无需模型 API"
    }
    private fun search(more: Boolean=false)=operation {
        val key=searchKey()
        require(!more || resultSearchKey==key) {"搜索条件已改变，请先重新搜索"}
        val offset=if(more)results.size else 0
        val args=arguments().apply {addProperty("query",query.trim());addProperty("mode",mode);addProperty("limit",50);addProperty("offset",offset);if(projectId.isNotBlank())addProperty("projectId",projectId)}
        val value=withContext(Dispatchers.IO){callTool("asset_search",args)}
        val items=value.getAsJsonArray("items").objects()
        results=if(more)(results+items).distinctBy {it.text("id")} else items
        total=if(serverId=="local-cloud"&&mode=="semantic")results.size else value.get("total")?.asInt ?: results.size
        transport=value.text("transport");hasSearched=true;resultSearchKey=key
    }
    private fun openAsset(asset: JsonObject)=operation {
        detail=asset;dependencies=null;preview=null;previewNotice=""
        val args=arguments().apply {addProperty("assetId",asset.text("id"))}
        val full=withContext(Dispatchers.IO){callTool("asset_get",args)}
        detail=full
        dependencies=withContext(Dispatchers.IO){runCatching {callTool("asset_dependencies",args)}.getOrNull()}
        loadPreview(full)
    }
    private suspend fun loadPreview(full:JsonObject) {
        if(full.text("kind")!="image")return
        if(serverId=="local-cloud"&&full.get("downloaded")?.asBoolean!=true){previewNotice="原素材保存在百度网盘，按需下载后可预览";return}
        previewNotice="正在读取图片预览…"
        val loaded=withContext(Dispatchers.IO){runCatching {
            val file=if(serverId=="local-cloud")cloud.cachedFile(full.text("id")) else null
            val bounds=BitmapFactory.Options().apply {inJustDecodeBounds=true}
            val bytes=if(file==null)client.thumbnail(full.text("id"),serverId) else null
            if(file!=null)BitmapFactory.decodeFile(file.absolutePath,bounds) else BitmapFactory.decodeByteArray(bytes!!,0,bytes.size,bounds)
            require(bounds.outWidth>0&&bounds.outHeight>0&&bounds.outWidth.toLong()*bounds.outHeight<=32_000_000){"预览尺寸过大"}
            var scale=1;while(maxOf(bounds.outWidth,bounds.outHeight)/scale>1600)scale*=2
            val options=BitmapFactory.Options().apply {inSampleSize=scale}
            (if(file!=null)BitmapFactory.decodeFile(file.absolutePath,options) else BitmapFactory.decodeByteArray(bytes!!,0,bytes.size,options))?:error("无法解码图片预览")
        }}
        loaded.onSuccess {preview=it;previewNotice=""}.onFailure {previewNotice=it.message?:"图片预览暂不可用"}
    }
    @Composable private fun Screen() {
        BackHandler(enabled=detail!=null){closeDetail()}
        Surface(Modifier.fillMaxSize().windowInsetsPadding(WindowInsets.safeDrawing),color=NativeDesignTokens.Background) {
            Column(Modifier.fillMaxSize()) {
                Row(Modifier.fillMaxWidth().padding(horizontal=12.dp,vertical=8.dp),verticalAlignment=Alignment.CenterVertically) {
                    TextButton(onClick={if(detail!=null)closeDetail() else finish()}){Text("返回")}
                    Text(if(detail==null)"游戏资源库" else "资源详情",style=MaterialTheme.typography.titleLarge,modifier=Modifier.weight(1f))
                    if(detail==null)TextButton(onClick={refreshServers()},enabled=!busy){Text("刷新")}
                }
                if(detail==null)TextButton(onClick={startActivity(Intent(this@ResourceLibraryActivity,NativeFileManagerActivity::class.java))},modifier=Modifier.padding(horizontal=16.dp)){Text("本地与网盘 · 文件管理")}
                if(busy)LinearProgressIndicator(Modifier.fillMaxWidth())
                if(workMessage.isNotBlank())Text(workMessage,color=NativeDesignTokens.Muted,style=MaterialTheme.typography.bodySmall,modifier=Modifier.padding(horizontal=20.dp,vertical=6.dp))
                if(notice.isNotBlank())Text(notice,color=MaterialTheme.colorScheme.error,style=MaterialTheme.typography.bodySmall,modifier=Modifier.fillMaxWidth().padding(16.dp))
                if(detail!=null)Detail(detail!!) else Browser()
                snapshots?.let {SnapshotDialog(it)}
                apiConfig?.let {ApiDialog(it)}
            }
        }
    }
    @Composable private fun Browser() {
        Column(Modifier.fillMaxSize()) {
            Column(Modifier.fillMaxWidth().background(NativeDesignTokens.Surface).padding(horizontal=20.dp,vertical=14.dp),verticalArrangement=Arrangement.spacedBy(12.dp)) {
                Row(Modifier.fillMaxWidth(),horizontalArrangement=Arrangement.spacedBy(8.dp)) {
                    Picker("资源来源",listOf("local-cloud" to "本机云资源库")+servers.map {it.text("deviceId") to it.text("name","电脑")},serverId,Modifier.weight(1f),{chooseServer(it)})
                    Picker("项目",listOf("" to "全部项目")+projects.map {it.text("id") to it.text("name","未命名项目")},projectId,Modifier.weight(1f),{projectId=it;results=emptyList();hasSearched=false})
                }
                OutlinedTextField(query,{query=it},Modifier.fillMaxWidth(),label={Text(if(mode=="semantic")"描述你想找的资源" else "名称、路径、文字或标签")},placeholder={Text(if(mode=="semantic")"例如：蓝色的塔防技能图标" else "例如：texture、ui、角色")},singleLine=true,enabled=!busy,keyboardOptions=KeyboardOptions(imeAction=ImeAction.Search),keyboardActions=KeyboardActions(onSearch={if(serverId.isNotBlank())search()}))
                Row(Modifier.fillMaxWidth(),verticalAlignment=Alignment.CenterVertically,horizontalArrangement=Arrangement.spacedBy(8.dp)) {
                    FilterChip(selected=mode=="text",onClick={mode="text"},enabled=!busy,label={Text("关键词")})
                    FilterChip(selected=mode=="semantic",onClick={mode="semantic"},enabled=!busy,label={Text("语义")})
                    Spacer(Modifier.weight(1f))
                    Button(onClick={search()},enabled=!busy&&serverId.isNotBlank()&&(mode=="text"||query.isNotBlank())){Text("搜索")}
                }
                if(serverId=="local-cloud")Row(horizontalArrangement=Arrangement.spacedBy(6.dp)) {
                    OutlinedButton(onClick={showSnapshots()},enabled=!busy){Text("从网盘恢复")}
                    TextButton(onClick={showApiConfig()},enabled=!busy&&cloudStatus.get("restored")?.asBoolean==true){Text("查询 API")}
                    TextButton(onClick={startActivity(Intent(this@ResourceLibraryActivity,BaiduNetdiskActivity::class.java))},enabled=!busy){Text("网盘账号")}
                }
                Text(if(serverId=="local-cloud")if(mode=="semantic")"查询文字发给外部向量 API，素材向量在手机比对；不运行模型。" else "分类和向量保存在本机，电脑离线也能搜索；素材按需下载。" else if(mode=="semantic")"由所选电脑调用已配置的模型 API，手机不运行模型。" else "搜索所选电脑已建立的资源索引。",style=MaterialTheme.typography.bodySmall,color=NativeDesignTokens.Muted)
            }
            if(serverId=="local-cloud"&&cloudStatus.get("restored")?.asBoolean!=true&&!busy){EmptyState("从云端带回你的资源库","点击“从网盘恢复”，一次下载分类、标签、依赖和已有向量，无需重新扫描或生成；原素材保持在网盘。先在“网盘账号”登录手机的百度账号。")}
            else if(serverId!="local-cloud"&&servers.isEmpty()&&!busy) {
                EmptyState("先连接资源电脑","在“我的设备”中配对 Mac，电脑资源库建立索引后即可在这里搜索。")
                TextButton(onClick={startActivity(Intent(this@ResourceLibraryActivity,NativeManagementActivity::class.java).putExtra("section","devices"))},modifier=Modifier.padding(horizontal=20.dp)){Text("打开我的设备")}
            } else if(serverId.isBlank()&&!busy)EmptyState("选择一台资源电脑","资源来自已配对的设备，连接地址会自动更新。")
            else if(results.isEmpty()&&!busy)EmptyState(if(hasSearched)"没有找到匹配资源" else "你的游戏素材，随时查找",if(hasSearched)"尝试名称、用途或资源描述，也可以切换搜索方式。" else "图片、模型、材质、动画与代码片段，使用同一份资源库。")
            else {
                Row(Modifier.fillMaxWidth().padding(horizontal=20.dp,vertical=10.dp),horizontalArrangement=Arrangement.SpaceBetween) {
                    Text((if(resultSearchKey!=searchKey())"上次搜索：" else "")+"${results.size} / $total 个资源",style=MaterialTheme.typography.labelMedium,color=NativeDesignTokens.Muted)
                    if(transport.isNotBlank())Text(if(serverId=="local-cloud")"手机本机索引" else if(transport=="lan")"局域网连接" else "中转连接",style=MaterialTheme.typography.labelMedium,color=NativeDesignTokens.Muted)
                }
                LazyColumn(Modifier.weight(1f).fillMaxWidth(),contentPadding=PaddingValues(horizontal=20.dp,vertical=4.dp)) {
                    items(results,key={it.text("id")}) {asset->AssetRow(asset)}
                    if(results.size<total&&resultSearchKey==searchKey())item {TextButton(onClick={search(true)},enabled=!busy,modifier=Modifier.fillMaxWidth()){Text("加载更多")}}
                }
            }
        }
    }
    @Composable private fun Picker(label: String,options: List<Pair<String,String>>,selected: String,modifier: Modifier,onSelect:(String)->Unit) {
        var expanded by remember {mutableStateOf(false)}
        Box(modifier) {
            OutlinedButton(onClick={expanded=true},enabled=!busy&&options.isNotEmpty(),modifier=Modifier.fillMaxWidth(),contentPadding=PaddingValues(horizontal=12.dp,vertical=10.dp)) {
                Column(Modifier.fillMaxWidth(),verticalArrangement=Arrangement.spacedBy(2.dp)) {
                    Text(label,style=MaterialTheme.typography.labelSmall,color=NativeDesignTokens.Muted)
                    Text(options.firstOrNull {it.first==selected}?.second ?: "请选择",maxLines=1,overflow=TextOverflow.Ellipsis,style=MaterialTheme.typography.labelLarge)
                }
            }
            DropdownMenu(expanded,{expanded=false}) {options.forEach {(id,name)->DropdownMenuItem(text={Text(name)},onClick={expanded=false;onSelect(id)})}}
        }
    }
    @Composable private fun EmptyState(title: String,description: String) {
        Column(Modifier.fillMaxWidth().padding(horizontal=24.dp,vertical=32.dp),verticalArrangement=Arrangement.spacedBy(10.dp)) {
            NativeNavIcon(1,NativeDesignTokens.Accent)
            Text(title,style=MaterialTheme.typography.titleLarge)
            Text(description,style=MaterialTheme.typography.bodyMedium,color=NativeDesignTokens.Muted)
        }
    }
    @Composable private fun AssetRow(asset: JsonObject) {
        Row(Modifier.fillMaxWidth().clickable(enabled=!busy){openAsset(asset)}.padding(vertical=14.dp),verticalAlignment=Alignment.CenterVertically) {
            Box(Modifier.size(48.dp).clip(RoundedCornerShape(10.dp)).background(NativeDesignTokens.Surface),contentAlignment=Alignment.Center) {NativeNavIcon(if(asset.text("kind")=="image")1 else 0,NativeDesignTokens.Accent)}
            Spacer(Modifier.width(12.dp))
            Column(Modifier.weight(1f),verticalArrangement=Arrangement.spacedBy(3.dp)) {
                Text(asset.text("name"),style=MaterialTheme.typography.titleSmall,maxLines=1,overflow=TextOverflow.Ellipsis)
                Text(asset.text("path"),style=MaterialTheme.typography.bodySmall,color=NativeDesignTokens.Muted,maxLines=1,overflow=TextOverflow.Ellipsis)
                Text((listOf(kind(asset.text("kind")),size(asset.get("bytes")?.asLong ?: 0))+(if(serverId=="local-cloud")listOf(if(asset.get("downloaded")?.asBoolean==true)"已下载" else "云端") else emptyList())).joinToString("  "),style=MaterialTheme.typography.labelSmall,color=NativeDesignTokens.Muted)
            }
            asset.get("score")?.takeIf {it.isJsonPrimitive && it.asJsonPrimitive.isNumber}?.let {Text(String.format(Locale.ROOT,"%.3f",it.asDouble),style=MaterialTheme.typography.labelSmall,color=NativeDesignTokens.Accent,modifier=Modifier.padding(start=8.dp))}
        }
        HorizontalDivider(color=NativeDesignTokens.Line)
    }
    @Composable private fun Detail(asset: JsonObject) {
        LazyColumn(Modifier.fillMaxSize(),contentPadding=PaddingValues(20.dp),verticalArrangement=Arrangement.spacedBy(18.dp)) {
            if(asset.text("kind")=="image")item {
                Box(Modifier.fillMaxWidth().height(270.dp).clip(RoundedCornerShape(12.dp)).background(NativeDesignTokens.Surface),contentAlignment=Alignment.Center) {
                    if(preview!=null)Image(preview!!.asImageBitmap(),contentDescription=asset.text("name"),modifier=Modifier.fillMaxSize().padding(12.dp),contentScale=ContentScale.Fit)
                    else Text(previewNotice.ifBlank {"读取资源预览"},color=NativeDesignTokens.Muted,style=MaterialTheme.typography.bodySmall,modifier=Modifier.padding(20.dp))
                }
            }
            item {Column(verticalArrangement=Arrangement.spacedBy(6.dp)) {
                Text(asset.text("name"),style=MaterialTheme.typography.headlineSmall)
                SelectionContainer {Text(asset.text("path"),style=MaterialTheme.typography.bodySmall,color=NativeDesignTokens.Muted)}
            }}
            item {NativeCard {
                Info("类型",kind(asset.text("kind")));Info("格式",asset.text("extension",asset.text("mimeType")));Info("大小",size(asset.get("bytes")?.asLong ?: 0))
                asset.getAsJsonObject("image")?.let {image->if(image.has("width")&&image.has("height"))Info("尺寸","${image.text("width")} × ${image.text("height")}")}
                Info("MD5",asset.text("md5"));Info("SHA-256",asset.text("sha256"))
            }}
            if(serverId=="local-cloud")item {Row(horizontalArrangement=Arrangement.spacedBy(8.dp)) {
                Button(onClick={downloadAsset(asset)},enabled=!busy&&asset.get("downloaded")?.asBoolean!=true&&asset.get("cloudAvailable")?.asBoolean==true){Text(if(asset.get("downloaded")?.asBoolean==true)"已下载" else "按需下载素材")}
                OutlinedButton(onClick={findSimilar(asset)},enabled=!busy&&(asset.getAsJsonArray("embeddings")?.size()?:0)>0){Text("查找相似")}
            }}
            val annotations=asset.getAsJsonArray("annotations").objects()
            if(annotations.isNotEmpty())item {NativeCard {
                Text("分类与说明",style=MaterialTheme.typography.titleMedium)
                annotations.forEach {annotation->
                    if(annotation.get("stale")?.asBoolean==true)Text("资源已变更，以下说明待更新",style=MaterialTheme.typography.labelSmall,color=MaterialTheme.colorScheme.error)
                    if(annotation.text("label").isNotBlank())Text(annotation.text("label"),fontWeight=FontWeight.Medium)
                    if(annotation.text("purpose").isNotBlank())Text(annotation.text("purpose"),style=MaterialTheme.typography.bodyMedium)
                    if(annotation.text("ocrText").isNotBlank())SelectionContainer {Text(annotation.text("ocrText"),style=MaterialTheme.typography.bodySmall)}
                    val categories=annotation.getAsJsonArray("categories")?.joinToString("、") {it.asString}.orEmpty()
                    if(categories.isNotBlank())Text(categories,style=MaterialTheme.typography.bodySmall,color=NativeDesignTokens.Muted)
                }
            }}
            dependencies?.let {data->item {NativeCard {
                Text("资源依赖",style=MaterialTheme.typography.titleMedium)
                val links=data.getAsJsonArray("items") ?: data.getAsJsonArray("edges") ?: JsonArray()
                val nodes=data.getAsJsonArray("nodes").objects().associateBy {it.text("id")}
                if(links.isEmpty)Text("未解析到依赖关系",style=MaterialTheme.typography.bodySmall,color=NativeDesignTokens.Muted)
                else links.take(30).forEach {link->val entry=link.asJsonObject;Text(nodes[entry.text("to")]?.text("path") ?: entry.text("targetPath",entry.text("path",entry.text("reference",entry.text("value")))),style=MaterialTheme.typography.bodySmall)}
                val unresolved=data.getAsJsonArray("unresolved")?.size() ?: 0
                if(unresolved>0)Text("另有 $unresolved 条引用尚未解析",style=MaterialTheme.typography.bodySmall,color=NativeDesignTokens.Muted)
            }}}
            item {Text(if(serverId=="local-cloud")"分类、依赖和已有向量已恢复到手机。原素材从百度网盘按需下载并校验，电脑无需在线；自然语言语义查询需手机单独配置外部 API Key。" else "文件保存在所选电脑，手机仅查看元数据和按需读取预览。模型 API 在电脑的资源库设置中配置。",style=MaterialTheme.typography.bodySmall,color=NativeDesignTokens.Muted)}
        }
    }
    @Composable private fun SnapshotDialog(rows:List<JsonObject>) {
        AlertDialog(onDismissRequest={if(!busy)snapshots=null},title={Text("恢复云资源库")},text={Column(verticalArrangement=Arrangement.spacedBy(8.dp)) {
            Text("仅恢复索引和向量，素材按需拉取。已生成向量无需重算。",style=MaterialTheme.typography.bodySmall)
            if(rows.isEmpty())Text("没有找到云快照。请先在电脑资源库上传分类与向量索引。")
            LazyColumn(Modifier.heightIn(max=340.dp)){items(rows){row->TextButton(onClick={restoreSnapshot(row.text("snapshotId"))},modifier=Modifier.fillMaxWidth()) {Column(Modifier.fillMaxWidth()){Text(row.text("snapshotId").take(8),style=MaterialTheme.typography.titleSmall);Text(java.text.SimpleDateFormat("yyyy-MM-dd HH:mm",Locale.getDefault()).format(java.util.Date((row.get("modifiedAt")?.asLong?:0)*1000)),style=MaterialTheme.typography.bodySmall)}}}}
        }},confirmButton={TextButton(onClick={snapshots=null}){Text("关闭")}})
    }
    @Composable private fun ApiDialog(data:JsonObject) {
        val profiles=data.getAsJsonArray("profiles").objects()
        var space by remember(data){mutableStateOf(profiles.firstOrNull()?.text("space")?:"")}
        var key by remember {mutableStateOf("")}
        var clear by remember {mutableStateOf(false)}
        val profile=profiles.firstOrNull {it.text("space")==space}
        AlertDialog(onDismissRequest={if(!busy)apiConfig=null},title={Text("手机查询模型 API")},text={Column(verticalArrangement=Arrangement.spacedBy(12.dp)) {
            Text("模型与地址沿用云索引的向量空间，手机只发查询文字。Key 仅加密保存在这台手机，不上传网盘。",style=MaterialTheme.typography.bodySmall)
            if(profiles.isEmpty())Text("云资源库尚无已生成向量的模型配置；关键词搜索仍可使用。")
            else {
                Picker("向量模型",profiles.map {it.text("space") to it.text("model")},space,Modifier.fillMaxWidth(),{space=it;key="";clear=false})
                Text(profile?.text("baseUrl").orEmpty(),style=MaterialTheme.typography.bodySmall)
                OutlinedTextField(key,{key=it},label={Text(if(profile?.get("hasApiKey")?.asBoolean==true)"API Key（留空保留）" else "API Key")},visualTransformation=PasswordVisualTransformation(),singleLine=true,modifier=Modifier.fillMaxWidth())
                Row(verticalAlignment=Alignment.CenterVertically){Checkbox(clear,{clear=it});Text("清除这个模型的 Key",style=MaterialTheme.typography.bodySmall)}
            }
        }},confirmButton={TextButton(onClick={saveApiConfig(space,key,clear)},enabled=!busy&&profile!=null){Text("保存")}},dismissButton={TextButton(onClick={apiConfig=null},enabled=!busy){Text("取消")}})
    }
    @Composable private fun Info(label: String,value: String) {if(value.isNotBlank())Column(verticalArrangement=Arrangement.spacedBy(2.dp)) {Text(label,style=MaterialTheme.typography.labelSmall,color=NativeDesignTokens.Muted);SelectionContainer {Text(value,style=MaterialTheme.typography.bodySmall)}}}
    private fun JsonObject.text(key: String,fallback: String="")=ResourceLibraryClient.field(this,key,fallback)
    private fun JsonArray?.objects(): List<JsonObject> = this?.mapNotNull {it.takeIf {value->value.isJsonObject}?.asJsonObject} ?: emptyList()
    private fun size(bytes: Long): String=when {bytes>=1024L*1024*1024->String.format(Locale.ROOT,"%.1f GB",bytes/(1024.0*1024*1024));bytes>=1024*1024->String.format(Locale.ROOT,"%.1f MB",bytes/(1024.0*1024));bytes>=1024->String.format(Locale.ROOT,"%.1f KB",bytes/1024.0);else->"$bytes B"}
    private fun kind(value: String)=mapOf("image" to "图片","model" to "模型","shader" to "Shader","material" to "材质","animation" to "动画","audio" to "音频","font" to "字体","spine" to "Spine","code" to "代码","scene" to "场景","prefab" to "预制体")[value] ?: value.ifBlank {"文件"}
}
