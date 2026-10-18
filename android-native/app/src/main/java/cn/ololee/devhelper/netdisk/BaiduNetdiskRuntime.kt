package cn.ololee.devhelper.netdisk

import android.content.Context
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import com.baidu.netdisk.ApaasBase
import com.baidu.netdisk.ApaasFile
import com.baidu.netdisk.ApaasTransmit
import com.baidu.netdisk.cloudfile.*
import com.baidu.netdisk.download.*
import com.baidu.netdisk.external.operation.*
import com.baidu.netdisk.kernel.architecture.debug.NetDiskLog
import com.baidu.netdisk.model.Account
import com.baidu.netdisk.model.AccountTokenInfo
import com.google.gson.*
import java.io.File
import java.util.UUID
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

/** Native SDK facade shared by the screen, HTTP and MCP; no SDK initialization until configured. */
class BaiduNetdiskRuntime private constructor(context: Context) {
    private val context = context.applicationContext
    private val secrets = BaiduNetdiskSecrets(this.context)
    private val main = Handler(Looper.getMainLooper())
    private val lock = Any()
    // The engine supplies an AtomicBoolean/lock-free check; never wait for its job lock here.
    @Volatile private var transferBusyProvider: () -> Boolean = { false }
    private var initialized = false
    private var downloadInitialized = false
    private var observersRegistered = false
    @Volatile private var tokenInvalid = false
    private val transfers = File(this.context.filesDir,"netdisk/downloads")
    private val indexFile = File(this.context.filesDir,"netdisk/download-index.json")
    private var index = if(indexFile.isFile) runCatching { JsonParser.parseString(indexFile.readText()).asJsonObject }.getOrDefault(JsonObject()) else JsonObject()
    companion object {
        @Volatile private var instance: BaiduNetdiskRuntime? = null
        @JvmStatic fun get(context: Context): BaiduNetdiskRuntime = instance ?: synchronized(this) {
            instance ?: BaiduNetdiskRuntime(context).also { instance = it }
        }
    }
    fun status(): JsonObject = BaiduNetdiskPolicy.publicConfig(secrets.read()).apply {
        addProperty("tokenInvalid",tokenInvalid);addProperty("sdkInitialized",initialized)
        addProperty("mcpPath","/mcp/netdisk")
        addProperty("message",if(get("configured").asBoolean)if(tokenInvalid)"百度授权已失效，请重新填写 Token" else "配置已保存，可连接网盘" else "请填写缺少的网盘配置；来源标识和资源根目录以百度应用接入信息为准，Token 仅加密存储在本机")
    }
    fun saveConfig(patch: JsonObject): JsonObject = synchronized(lock) {
        check(!transferBusyProvider()) { "本地与网盘传输正在进行，请完成或取消任务后修改网盘配置" }
        val next = BaiduNetdiskPolicy.merge(secrets.read(),patch)
        if(initialized) onMain { ApaasBase.instance.uninit() }
        initialized=false; downloadInitialized=false;tokenInvalid=false
        secrets.write(next)
        status()
    }
    internal fun registerTransferBusyProvider(provider: () -> Boolean) { transferBusyProvider = provider }
    // Configuration → job-lock is the sole lock order. The supplied busy provider is lock-free.
    internal fun <T> withTransferConfigurationGuard(block: () -> T): T = synchronized(lock) { block() }
    internal fun binaryClient(): NativeTransferCloud = synchronized(lock) {
        val config = secrets.read()
        check(!tokenInvalid) { "百度授权已失效，请重新授权" }
        BaiduBinaryFileClient(config)
    }
    internal fun nativeTransfers(): NativeFileTransferRuntime = NativeFileTransferRuntime.get(context)
    fun authorization(): JsonObject {
        val config=secrets.read();val client=BaiduNetdiskPolicy.get(config,"clientId")
        require(client.isNotBlank()) { "请先填写 API Key（clientId）" }
        val state=UUID.randomUUID().toString()
        val url=Uri.parse("https://openapi.baidu.com/oauth/2.0/authorize").buildUpon()
            .appendQueryParameter("response_type","code").appendQueryParameter("client_id",client)
            .appendQueryParameter("redirect_uri",BaiduNetdiskPolicy.get(config,"redirectUri"))
            .appendQueryParameter("scope","basic,netdisk").appendQueryParameter("display","mobile")
            .appendQueryParameter("state",state).build().toString()
        return JsonObject().apply {addProperty("authorizationUrl",url);addProperty("state",state);addProperty("message","在百度授权后，使用受信任电脑或服务端换取 access_token/refresh_token，再在本页填入；不把 Secret Key 分发到 APK。")}
    }
    private fun ready(): JsonObject = synchronized(lock) {
        val config=secrets.read();val projection=BaiduNetdiskPolicy.publicConfig(config)
        check(projection.get("configured").asBoolean) {"百度网盘未配置，缺少: "+projection.getAsJsonArray("missingFields").joinToString(", "){it.asString}}
        check(!tokenInvalid) { "百度授权已失效，请重新填写 Token" }
        if(!initialized) {
            onMain {
                NetDiskLog.setDebug(false)
                val base=ApaasBase.instance
                base.setAccountInfo(Account(BaiduNetdiskPolicy.get(config,"appId"),BaiduNetdiskPolicy.get(config,"thirdId"),BaiduNetdiskPolicy.get(config,"from"),BaiduNetdiskPolicy.get(config,"remoteRoot"),Settings.Secure.getString(context.contentResolver,Settings.Secure.ANDROID_ID).orEmpty()))
                base.setToken(AccountTokenInfo(BaiduNetdiskPolicy.get(config,"accessToken"),BaiduNetdiskPolicy.get(config,"refreshToken")))
                base.setTokenInvalidCallback(object : ITransmitterCallBack<AccountTokenInfo> { override fun onResult(result: TransmitterResult<AccountTokenInfo>) { tokenInvalid=true } })
                base.setNetworkClient(BaiduNetdiskHttpClient())
                if(!observersRegistered) {
                    base.registerLifecycleObserver(ApaasFile.instance)
                    base.registerLifecycleObserver(ApaasTransmit.instance)
                    observersRegistered=true
                }
                base.init(context)
                ApaasTransmit.instance.setUploadConfig(UploadConfig(2,-1,DuplicateStrategy.NEW_NAME))
            }
            initialized=true
        }
        config
    }
    fun files(args: JsonObject): JsonObject {
        val config=ready();val dir=BaiduNetdiskPolicy.insideRoot(config,text(args,"path",BaiduNetdiskPolicy.get(config,"remoteRoot")))
        val offset=integer(args,"offset",0,0,Int.MAX_VALUE);val limit=integer(args,"limit",100,1,1000)
        val response=await<CloudFileListInfo> { callback -> ApaasFile.instance.getFileList(FileListRequest().apply {this.dir=dir;start=offset;num=limit},callback) }
        val rows=JsonArray();response?.sdkCloudFileResponse?.list?.forEach { item ->
            if(runCatching {BaiduNetdiskPolicy.insideRoot(config,item.path)}.isSuccess) rows.add(JsonObject().apply {
                addProperty("fsId",item.fs_id.toString());addProperty("name",item.server_filename);addProperty("path",item.path)
                addProperty("bytes",item.size);addProperty("modifiedAt",item.server_mtime);addProperty("isDirectory",item.isdir==1)
                addProperty("category",item.category);addProperty("md5",item.md5)
            })
        }
        return JsonObject().apply {add("files",rows);addProperty("path",dir);addProperty("offset",offset);addProperty("limit",limit);addProperty("hasMore",response?.sdkCloudFileResponse?.hasMore?:false)}
    }
    fun mkdir(args: JsonObject): JsonObject {
        val config=ready();val path=BaiduNetdiskPolicy.insideRoot(config,text(args,"path"))
        val response=await<CreateDirResponse> {ApaasFile.instance.createDir(CreateDirRequest("",path),it)}
        return JsonObject().apply {addProperty("path",path);addProperty("fsId",response?.mFsId?.toString());addProperty("created",true)}
    }
    fun upload(args: JsonObject): JsonObject {
        val local=text(args,"localPath")
        val uri=if(local.startsWith("content://"))Uri.parse(local) else Uri.fromFile(File(local).canonicalFile.also {require(it.isFile&&it.canRead()){ "手机文件不存在或不可读取，请用原生页面选择文件" }})
        return uploadUris(listOf(uri),text(args,"remoteDir",BaiduNetdiskPolicy.get(secrets.read(),"remoteRoot")))
    }
    fun uploadUris(uris: List<Uri>, remoteDir: String): JsonObject {
        require(uris.isNotEmpty()) {"请选择上传文件"};val config=ready();val dir=BaiduNetdiskPolicy.insideRoot(config,remoteDir)
        uris.forEach {require(it.scheme=="content"||it.scheme=="file"){"只接受手机本地文件"}}
        await<Nothing> {ApaasTransmit.instance.startUpload(uris,dir,false,it)}
        return JsonObject().apply {addProperty("queued",true);addProperty("fileCount",uris.size);addProperty("remoteDir",dir);addProperty("encoding","binary");addProperty("jobsPath","/api/netdisk/jobs");addProperty("duplicatePolicy","new_name")}
    }
    private fun readyDownload() {
        ready();synchronized(lock) {if(!downloadInitialized) {await<Nothing> {ApaasTransmit.instance.initDownload(context,it)};downloadInitialized=true}}
    }
    fun download(args: JsonObject): JsonObject {
        val config=ready();val fsid=text(args,"fsId").toLongOrNull()?:throw IllegalArgumentException("fsId 无效")
        require(fsid>0){"fsId 无效"}
        val meta=await<FileMetaResult> {ApaasFile.instance.getFileMeta(FileMetaRequest(listOf(fsid),null,false,1,false),it)}?.data?.list?.firstOrNull()?:error("网盘文件不存在")
        BaiduNetdiskPolicy.insideRoot(config,meta.path);require(meta.isDir==0){"请逐个下载文件，目录请先列出内容"}
        val filename=BaiduNetdiskPolicy.fileName(meta.server_filename)
        readyDownload();transfers.mkdirs();val destination=File(transfers,UUID.randomUUID().toString()+"/"+filename);destination.parentFile!!.mkdirs()
        val task=await<UserDownloadTask> {ApaasTransmit.instance.addDownloadTask(UserDownloadTask(fsid,meta.size,destination.absolutePath,filename,meta.category,"",-1,DownloadStatus.UNRESOLVED),it)}?:error("下载任务未创建")
        require(task.taskId>=0){"下载任务未创建"}
        synchronized(lock){index.add(task.taskId.toString(),JsonObject().apply {addProperty("localPath",destination.absolutePath);addProperty("name",filename);addProperty("bytes",meta.size);addProperty("remotePath",meta.path);addProperty("fsId",fsid.toString())});persistIndex()}
        return JsonObject().apply {addProperty("jobId","download:${task.taskId}");addProperty("state","queued");addProperty("name",filename);addProperty("bytes",meta.size)}
    }
    fun jobs(): JsonObject {
        ready();val rows=JsonArray()
        await<List<com.baidu.netdisk.transmit.UploadTaskModel>> {ApaasTransmit.instance.getAllUploadTasks(it)}?.forEach {task ->rows.add(JsonObject().apply {
            addProperty("jobId","upload:${task.taskId}");addProperty("kind","upload");addProperty("name",task.fileName)
            addProperty("state",when(task.state){0->"running";1->"completed";2->"paused";-2->"cancelled";else->"failed"})
            addProperty("bytes",task.fileSize);addProperty("transferredBytes",task.offsetSize);addProperty("bytesPerSecond",task.rate);addProperty("errorCode",task.errorCode)
            addProperty("remotePath",task.taskRemotePath)
        })}
        readyDownload()
        await<List<UserDownloadInfo>> {ApaasTransmit.instance.getDownloadTaskInfos(DownloadStatus.ALL,it)}?.forEach {task ->rows.add(downloadJob(task))}
        return JsonObject().apply {add("jobs",rows);addProperty("encoding","binary")}
    }
    fun job(jobId: String): JsonObject {
        val pair=parseJob(jobId);ready()
        if(pair.first=="download") {readyDownload();return downloadJob(await<UserDownloadInfo> {ApaasTransmit.instance.getDownloadTaskInfo(pair.second,it)}?:error("下载任务不存在"))}
        return jobs().getAsJsonArray("jobs").firstOrNull {it.asJsonObject.get("jobId").asString==jobId}?.asJsonObject?:error("上传任务不存在")
    }
    private fun downloadJob(task: UserDownloadInfo): JsonObject {
        val saved=synchronized(lock){index.getAsJsonObject(task.taskId.toString())?.deepCopy()}
        return JsonObject().apply {
            addProperty("jobId","download:${task.taskId}");addProperty("kind","download")
            addProperty("state",when(task.status){DownloadStatus.FINISH->"completed";DownloadStatus.RUNNING->"running";DownloadStatus.PAUSE->"paused";DownloadStatus.FAIL->"failed";else->"queued"})
            addProperty("bytes",task.fileSize);addProperty("transferredBytes",task.downloadedSize);addProperty("bytesPerSecond",task.downloadSpeed);addProperty("errorCode",task.errorCode)
            saved?.let {addProperty("name",BaiduNetdiskPolicy.get(it,"name"));addProperty("remotePath",BaiduNetdiskPolicy.get(it,"remotePath"))}
            if(task.status==DownloadStatus.FINISH&&saved!=null)addProperty("contentPath","/api/netdisk/jobs/download:${task.taskId}/content")
        }
    }
    fun action(jobId: String, action: String): JsonObject {
        val pair=parseJob(jobId);ready();require(action in listOf("pause","resume","cancel")){"操作无效"}
        if(pair.first=="upload") await<Nothing> {when(action){"pause"->ApaasTransmit.instance.pauseUpload(listOf(pair.second),it);"resume"->ApaasTransmit.instance.resumeUpload(0,listOf(pair.second),it);else->ApaasTransmit.instance.deleteUpload(listOf(pair.second),it)}}
        else {readyDownload();await<Nothing> {when(action){"pause"->ApaasTransmit.instance.pauseDownloadTask(pair.second,it);"resume"->ApaasTransmit.instance.startDownloadTask(pair.second,it);else->ApaasTransmit.instance.deleteDownloadTask(pair.second,false,it)}}}
        return JsonObject().apply {addProperty("jobId",jobId);addProperty("action",action);addProperty("accepted",true)}
    }
    fun content(jobId: String): File {
        val pair=parseJob(jobId);require(pair.first=="download"){"只有下载任务提供文件"}
        check(job(jobId).get("state").asString=="completed"){"下载尚未完成"}
        val saved=synchronized(lock){index.getAsJsonObject(pair.second.toString())?.deepCopy()}?:error("下载记录不存在")
        val file=File(BaiduNetdiskPolicy.get(saved,"localPath")).canonicalFile
        require(file.path.startsWith(transfers.canonicalPath+File.separator)&&file.isFile){"下载文件不存在"}
        check(file.length()==saved.get("bytes").asLong){"下载文件大小校验未通过"};return file
    }
    private fun persistIndex(){indexFile.parentFile!!.mkdirs();val temp=File(indexFile.parentFile,"download-index.pending");temp.writeText(index.toString());check(temp.renameTo(indexFile)){"下载记录保存失败"}}
    private fun parseJob(id: String): Pair<String,Int> {val m=Regex("(upload|download):([0-9]{1,10})").matchEntire(id)?:throw IllegalArgumentException("网盘任务 ID 无效");return m.groupValues[1] to (m.groupValues[2].toIntOrNull()?:throw IllegalArgumentException("网盘任务 ID 无效"))}
    private fun onMain(block: ()->Unit) {if(Looper.myLooper()==Looper.getMainLooper())block() else {val latch=CountDownLatch(1);val failure=AtomicReference<Throwable>();main.post {try{block()}catch(t:Throwable){failure.set(t)}finally{latch.countDown()}};check(latch.await(15,TimeUnit.SECONDS)){"网盘初始化超时"};failure.get()?.let {throw it}}}
    private fun <T> await(start: (ITransmitterCallBack<T>)->Unit): T? {
        check(Looper.myLooper()!=Looper.getMainLooper()){ "网盘操作需要在后台线程调用" }
        val latch=CountDownLatch(1);val result=AtomicReference<TransmitterResult<T>>()
        start(object : ITransmitterCallBack<T> { override fun onResult(value: TransmitterResult<T>) { result.set(value);latch.countDown() } })
        check(latch.await(90,TimeUnit.SECONDS)){"百度网盘操作超时，请检查网络和任务列表"}
        val response=result.get()?:error("百度网盘未返回结果");check((response.errorInfo?.errorCode?:0)==0){"百度网盘 SDK 错误码: ${response.errorInfo?.errorCode}；请检查应用权限、账号授权和网络"}
        return response.data
    }
    private fun text(args:JsonObject,key:String,fallback:String?=null):String {if(!args.has(key))return fallback?:throw IllegalArgumentException("缺少参数: $key");val value=args.get(key);require(value.isJsonPrimitive&&value.asJsonPrimitive.isString){"$key 必须为文本"};return value.asString.also {require(it.length<=8192&&!it.any {c->c.code<32||c.code==127}){"$key 无效"}}}
    private fun integer(args:JsonObject,key:String,fallback:Int,min:Int,max:Int):Int {if(!args.has(key))return fallback;val value=runCatching {args.get(key).asBigDecimal.intValueExact()}.getOrElse {throw IllegalArgumentException("$key 必须为整数")};require(value in min..max){"$key 超出范围"};return value}
}
