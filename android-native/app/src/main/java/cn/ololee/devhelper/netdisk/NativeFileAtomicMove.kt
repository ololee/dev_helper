package cn.ololee.devhelper.netdisk

/** Directory-relative Linux NOREPLACE; no overwrite-capable fallback is permitted. */
internal object NativeFileAtomicMove {
    private val available=runCatching {System.loadLibrary("mcp_file_ops");true}.getOrDefault(false)
    private external fun move(parent:ByteArray,source:ByteArray,target:ByteArray,device:Long,inode:Long):Int
    private external fun commit(sourceParent:ByteArray,targetParent:ByteArray,source:ByteArray,target:ByteArray,device:Long,inode:Long):Int
    fun rename(parent:String,source:String,target:String,device:Long,inode:Long) {
        check(available) {"当前设备不支持安全重命名，未修改源文件"}
        NativeTransferEngine.safeName(source);NativeTransferEngine.safeName(target)
        checked(move(parent.toByteArray(Charsets.UTF_8),source.toByteArray(Charsets.UTF_8),target.toByteArray(Charsets.UTF_8),device,inode),false)
    }
    fun commitPreview(sourceParent:String,targetParent:String,source:String,target:String,device:Long,inode:Long) {
        check(available) {"当前设备不支持安全预览缓存提交"}
        NativeTransferEngine.safeName(source);NativeTransferEngine.safeName(target)
        checked(commit(sourceParent.toByteArray(Charsets.UTF_8),targetParent.toByteArray(Charsets.UTF_8),source.toByteArray(Charsets.UTF_8),target.toByteArray(Charsets.UTF_8),device,inode),true)
    }
    private fun checked(code:Int,preview:Boolean) {
        if(!preview&&code in setOf(22,38,95))throw NativeRenameUnsupportedException(code)
        check(code==0) {when(code) {17->"目标已存在同名项目，未覆盖";116->"源文件身份已改变，未执行";18,38,95->if(preview)"私有文件系统不支持安全预览提交，未修改已有文件" else "设备文件系统不支持安全重命名，未修改源文件";else->"安全${if(preview)"预览提交" else "重命名"}失败（系统代码 $code），请检查实际文件状态"}}
    }
}

internal class NativeRenameUnsupportedException(val errno:Int):UnsupportedOperationException("当前文件系统不支持原子无覆盖重命名（系统代码 $errno）")
