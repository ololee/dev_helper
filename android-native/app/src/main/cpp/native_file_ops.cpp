#include <jni.h>
#include <cerrno>
#include <cstdint>
#include <fcntl.h>
#include <string>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

#ifndef RENAME_NOREPLACE
#define RENAME_NOREPLACE (1 << 0)
#endif

static bool bytes(JNIEnv* env, jbyteArray value, std::string& out, bool name) {
    if (!value) return false;
    const jsize length=env->GetArrayLength(value);
    if (length<=0 || length>(name?1024:32768)) return false;
    out.resize(static_cast<size_t>(length));
    env->GetByteArrayRegion(value,0,length,reinterpret_cast<jbyte*>(&out[0]));
    if (env->ExceptionCheck() || out.find('\0')!=std::string::npos) return false;
    if (name && (out=="." || out==".." || out.find('/')!=std::string::npos || out.find('\\')!=std::string::npos)) return false;
    return name || out[0]=='/';
}
static int noReplace(const std::string& sourceDirectory,const std::string& targetDirectory,const std::string& oldName,const std::string& newName,jlong device,jlong inode,bool onlyRegular) {
    const int sourceFd=open(sourceDirectory.c_str(),O_RDONLY|O_DIRECTORY|O_CLOEXEC|O_NOFOLLOW);
    if(sourceFd<0)return errno;
    const int targetFd=open(targetDirectory.c_str(),O_RDONLY|O_DIRECTORY|O_CLOEXEC|O_NOFOLLOW);
    if(targetFd<0) {const int result=errno;close(sourceFd);return result;}
    struct stat before{};
    int result=0;
    if(fstatat(sourceFd,oldName.c_str(),&before,AT_SYMLINK_NOFOLLOW)<0)result=errno;
    else if((!S_ISREG(before.st_mode)&&(onlyRegular||!S_ISDIR(before.st_mode)))||static_cast<uint64_t>(before.st_dev)!=static_cast<uint64_t>(device)||static_cast<uint64_t>(before.st_ino)!=static_cast<uint64_t>(inode))result=ESTALE;
    else {
#ifdef __NR_renameat2
        if(syscall(__NR_renameat2,sourceFd,oldName.c_str(),targetFd,newName.c_str(),RENAME_NOREPLACE)!=0)result=errno;
#else
        result=ENOSYS;
#endif
    }
    close(targetFd);close(sourceFd);
    return result;
}
extern "C" JNIEXPORT jint JNICALL
Java_cn_ololee_devhelper_netdisk_NativeFileAtomicMove_move(JNIEnv* env,jobject,jbyteArray parent,jbyteArray source,jbyteArray target,jlong device,jlong inode) {
    std::string directory,oldName,newName;
    if(!bytes(env,parent,directory,false)||!bytes(env,source,oldName,true)||!bytes(env,target,newName,true))return EINVAL;
    return noReplace(directory,directory,oldName,newName,device,inode,false);
}
extern "C" JNIEXPORT jint JNICALL
Java_cn_ololee_devhelper_netdisk_NativeFileAtomicMove_commit(JNIEnv* env,jobject,jbyteArray sourceParent,jbyteArray targetParent,jbyteArray source,jbyteArray target,jlong device,jlong inode) {
    std::string sourceDirectory,targetDirectory,oldName,newName;
    if(!bytes(env,sourceParent,sourceDirectory,false)||!bytes(env,targetParent,targetDirectory,false)||!bytes(env,source,oldName,true)||!bytes(env,target,newName,true))return EINVAL;
    return noReplace(sourceDirectory,targetDirectory,oldName,newName,device,inode,true);
}
