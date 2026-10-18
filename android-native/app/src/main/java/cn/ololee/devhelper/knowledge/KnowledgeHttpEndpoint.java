package cn.ololee.devhelper.knowledge;

import android.content.Context;
import com.google.gson.*;
import com.google.gson.stream.JsonReader;
import com.google.gson.stream.JsonToken;
import io.github.libsumcp.protocol.HttpEndpoint;
import io.github.libsumcp.protocol.FileHttpResponse;
import java.io.*;
import java.net.URLDecoder;
import java.nio.ByteBuffer;
import java.nio.charset.*;
import java.util.*;
import java.util.function.Supplier;

/** Offline management page and bounded JSON API sharing the HTTP MCP listener. */
public final class KnowledgeHttpEndpoint implements HttpEndpoint {
    private final KnowledgeRuntime runtime;
    private final Context appContext;
    private final Supplier<JsonArray> tools;
    private final byte[] page;
    private final byte[] markdownParser;
    public KnowledgeHttpEndpoint(Context context, KnowledgeRuntime runtime, Supplier<JsonArray> tools) throws IOException {
        this.runtime = runtime; this.tools = tools; this.appContext = context.getApplicationContext();
        try (InputStream input = context.getAssets().open("knowledge.html"); ByteArrayOutputStream bytes = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[8192]; int count;
            while ((count = input.read(buffer)) != -1) {
                bytes.write(buffer,0,count);
                if (bytes.size() > 512 * 1024) throw new IOException("Management page exceeds limit");
            }
            page = bytes.toByteArray();
        }
        try (InputStream input=context.getAssets().open("vendor/markdown-it.min.js"); ByteArrayOutputStream bytes=new ByteArrayOutputStream()) {
            byte[] buffer=new byte[8192]; int count;
            while((count=input.read(buffer))!=-1) {
                bytes.write(buffer,0,count);
                if(bytes.size()>256*1024)throw new IOException("Markdown parser exceeds limit");
            }
            markdownParser=bytes.toByteArray();
        }
    }
    @Override public boolean handles(String target) {
        String path = target.split("\\?",2)[0];
        return path.equals("/") || path.equals("/index.html") || path.equals("/notes") || path.equals("/notes/") || path.startsWith("/api/knowledge/") || path.startsWith("/api/workflows/") || path.startsWith("/api/relay/") || path.startsWith("/api/netdisk/");
    }
    @Override public int maxRequestBytes(String target) {
        if (target.split("\\?",2)[0].equals("/api/knowledge/attachments/upload")) return 128*1024*1024;
        return target.equals("/api/knowledge/vectors/import") ? 4*1024*1024 : 256*1024;
    }
    @Override public long maxStreamingRequestBytes(String target) {
        String path = target.split("\\?",2)[0];
        if (miniGameIconPath(path)) return 2L * 1024 * 1024;
        return path.equals("/api/knowledge/attachments/upload") || syncAttachmentPath(path) ? Long.MAX_VALUE : maxRequestBytes(target);
    }
    @Override public boolean streamsRequest(String method,String target) {
        String path = target.split("\\?",2)[0];
        return method.equals("POST") && (path.equals("/api/knowledge/attachments/upload") || syncAttachmentPath(path) || miniGameIconPath(path));
    }
    @Override public Response handleStream(String method,String target,Map<String,String> headers,InputStream body,long length) throws IOException {
        try {
            if(!streamsRequest(method,target) || target.length()>4096) return error(400,"Invalid upload URL");
            String[] parts=target.split("\\?",2); Map<String,String> query=query(parts.length==2?parts[1]:"");
            if (miniGameIconPath(parts[0])) {
                knownQuery(query);
                String mime=headers.getOrDefault("content-type","").split(";",2)[0].trim().toLowerCase(Locale.ROOT);
                if (!Arrays.asList("image/png","image/jpeg","image/webp","application/octet-stream").contains(mime)) return error(415,"请选择 PNG、JPEG 或 WebP 图标");
                try (ByteArrayOutputStream image=new ByteArrayOutputStream()) {
                    byte[] buffer=new byte[8192];int n;
                    while((n=body.read(buffer))!=-1) {if(n>2*1024*1024-image.size())return error(413,"图标超过 2 MiB");image.write(buffer,0,n);}
                    return MiniGameHttpEndpoint.handle(runtime,method,target,headers,image.toByteArray());
                }
            }
            boolean sync = syncAttachmentPath(parts[0]);
            knownQuery(query, sync ? new String[]{"name", "sha256"} : new String[]{"name"});
            String mime=headers.getOrDefault("content-type","application/octet-stream").split(";",2)[0].trim();
            if (mime.length() > 160 || !mime.matches("[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+")) return error(415,"Invalid file content type");
            if (sync) {
                if (!query.containsKey("sha256")) return error(400, "sha256 is required for attachment sync");
                String id = identifier(parts[0].substring("/api/knowledge/sync/attachments/".length()));
                return json(200, runtime.attachments().importSyncStream(body, id, query.get("sha256"), query.getOrDefault("name", "media"), mime, length));
            }
            return json(200,runtime.attachments().importStream(body,query.getOrDefault("name","media"),mime,length));
        } catch(IllegalStateException conflict) { return error(409,KnowledgeRuntime.message(conflict)); }
        catch(IllegalArgumentException invalid) { return error(400,KnowledgeRuntime.message(invalid)); }
    }
    static boolean miniGameIconPath(String path) { return path.matches("/api/knowledge/minigames/games/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/icon"); }
    private static Response download(Response response) {
        String extra=response.extraHeaders.replace("Content-Disposition: inline;","Content-Disposition: attachment;")+"X-Content-Type-Options: nosniff\r\n";
        return response.stream==null?new Response(response.status,response.contentType,response.body,extra):new Response(response.status,response.contentType,response.stream,response.contentLength,extra);
    }
    static Response managementPageResponse(byte[] content, boolean embedsAssistant) {
        // Both the HTTP policy and knowledge.html's policy must permit the local AI frame.
        // Only the notes page can be embedded; the management page stays a top-level page.
        String framing=embedsAssistant?"frame-src 'self'; frame-ancestors 'none'":"frame-src 'none'; frame-ancestors 'self'";
        return new Response(200,"text/html; charset=utf-8",content,
                "Content-Security-Policy: default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; media-src 'self' blob:; base-uri 'none'; "+framing+"; form-action 'none'\r\n");
    }
    @Override public Response handle(String method, String target, Map<String,String> headers, byte[] body) {
        try {
            if (target.length() > 4096) return error(400,"URL exceeds limit");
            Response fileTransfer = cn.ololee.devhelper.netdisk.NativeFileTransferHttpEndpoint.handle(appContext,method,target,body);
            if (fileTransfer != null) return fileTransfer;
            Response assets = cn.ololee.devhelper.assets.ResourceLibraryHttpEndpoint.handle(appContext,method,target,body);
            if (assets != null) return assets;
            Response netdisk = cn.ololee.devhelper.netdisk.BaiduNetdiskHttpEndpoint.handle(cn.ololee.devhelper.netdisk.BaiduNetdiskRuntime.get(appContext),method,target,headers,body);
            if (netdisk != null) return netdisk;
            if (target.startsWith("/api/relay/")) return cn.ololee.devhelper.relay.RelayClient.get(appContext).localRequest(method,target,headers,body);
            Response minigame = MiniGameHttpEndpoint.handle(runtime,method,target,headers,body);
            if (minigame != null) return minigame;
            Response spine = SpineHttpEndpoint.handle(appContext,runtime,method,target,headers,body);
            if (spine != null) return spine;
            Response notification = TaskNotificationEndpoint.handle(appContext,method,target,body);
            if (notification != null) return notification;
            Response workflow = WorkflowEndpoint.get(appContext, runtime).handle(method, target, headers, body);
            if (workflow != null) return workflow;
            String[] parts=target.split("\\?",2); String path=parts[0];
            Map<String,String> query=query(parts.length==2?parts[1]:"");
            if (path.equals("/notes") || path.equals("/notes/")) {
                if (!method.equals("GET") && !method.equals("HEAD")) return method("GET, HEAD"); knownQuery(query);
                byte[] notes;
                try (InputStream input = appContext.getAssets().open("notes.html"); ByteArrayOutputStream output = new ByteArrayOutputStream()) {
                    byte[] buffer = new byte[8192]; int n;
                    while ((n=input.read(buffer)) != -1) { output.write(buffer,0,n); if (output.size()>512*1024) throw new IOException("Notes page exceeds limit"); }
                    notes=output.toByteArray();
                }
                return managementPageResponse(notes,false);
            }
            if (path.equals("/") || path.equals("/index.html")) {
                if (!method.equals("GET") && !method.equals("HEAD")) return method("GET, HEAD");
                return managementPageResponse(page,true);
            }
            String base="/api/knowledge/";
            if (!path.startsWith(base)) return error(404,"Endpoint not found");
            String route=path.substring(base.length());
            if(route.equals("resources/batch-delete")||route.equals("resources/batch-export")) {
                knownQuery(query);if(!method.equals("POST"))return method("POST");JsonObject fields=parse(body,32768);
                return json(route.endsWith("export")?202:200,route.endsWith("export")?runtime.startResourceBatchExport(fields):runtime.deleteResourceBatch(fields));
            }
            if(route.startsWith("resources/batch-export/")) {
                knownQuery(query);String[] values=route.substring("resources/batch-export/".length()).split("/",-1);String id=identifier(values[0]);
                if(values.length==1){if(!method.equals("GET"))return method("GET");JsonObject fields=new JsonObject();fields.addProperty("jobId",id);return json(200,runtime.resourceBatchExportStatus(fields));}
                if(values.length==2&&values[1].equals("content")){if(!method.equals("GET")&&!method.equals("HEAD"))return method("GET, HEAD");return download(FileHttpResponse.open(runtime.resourceBatchExportFile(id),"application/zip","resources-"+id+".zip",method,headers.get("range")));}
                return error(404,"Resource export endpoint not found");
            }
            if (route.startsWith("recording/")) {
                knownQuery(query);
                if (route.equals("recording/status")) { if (!method.equals("GET")) return method("GET"); return json(200,runtime.audioRecording().status()); }
                if (!route.equals("recording/start") && !route.equals("recording/stop")) return error(404,"Recording endpoint not found");
                if (!method.equals("POST")) return method("POST");
                JsonObject fields=parse(body,4096);
                try {
                    JsonObject recording;
                    if (route.equals("recording/start")) recording=runtime.audioRecording().start(fields);
                    else {
                        KnowledgeRuntime.AudioRecordingStopRequest request=KnowledgeRuntime.audioRecordingStopArguments(fields);
                        recording=request.cancelCountdown ? runtime.audioRecording().cancelCountdown(request.recordingId)
                                : request.recordingId == null ? runtime.audioRecording().stop() : runtime.audioRecording().stop(request.recordingId);
                    }
                    return json("failed".equals(recording.get("state").getAsString()) ? 400 : 200, recording);
                } catch (AudioRecordingManager.RecordingError denied) { return json(denied.httpStatus,denied.response(runtime.audioRecording().status())); }
            }
            if (route.equals("sync/manifest")) {
                if (!method.equals("GET")) return method("GET"); knownQuery(query);
                byte[] encoded = runtime.documents().syncManifest().toString().getBytes(StandardCharsets.UTF_8);
                return new Response(200, "application/json; charset=utf-8", new ByteArrayInputStream(encoded), encoded.length, "");
            }
            if (route.startsWith("sync/records/")) {
                if (!method.equals("GET")) return method("GET"); knownQuery(query);
                return json(200, runtime.documents().syncRecord(identifier(route.substring("sync/records/".length()))));
            }
            if (route.equals("sync/apply")) {
                if (!method.equals("POST")) return method("POST"); knownQuery(query);
                return json(200, runtime.applySyncDocument(parse(body, 256 * 1024)));
            }
            if (route.startsWith("sync/attachments/")) return method("POST");
            if(route.equals("assets/markdown-it.min.js")) {
                if(!method.equals("GET") && !method.equals("HEAD"))return method("GET, HEAD");
                knownQuery(query);
                return new Response(200,"text/javascript; charset=utf-8",markdownParser);
            }
            if(route.equals("assets/motion.js") || route.equals("assets/motion.css")
                    || route.equals("assets/minigames/index.html") || route.equals("assets/minigames/minigames-ui.js")
                    || route.equals("assets/minigames/minigames-ui.css")) {
                if(!method.equals("GET") && !method.equals("HEAD"))return method("GET, HEAD");
                knownQuery(query);
                String asset=route.substring("assets/".length());
                try(InputStream input=appContext.getAssets().open(asset); ByteArrayOutputStream bytes=new ByteArrayOutputStream()) {
                    byte[] buffer=new byte[8192]; int count;
                    while((count=input.read(buffer))!=-1) {
                        bytes.write(buffer,0,count);
                        if(bytes.size()>128*1024)throw new IOException("Bundled UI asset exceeds limit");
                    }
                    return new Response(200,asset.endsWith(".js")?"text/javascript; charset=utf-8":asset.endsWith(".html")?"text/html; charset=utf-8":"text/css; charset=utf-8",bytes.toByteArray());
                }
            }
            if (route.equals("status")) return get(method,runtime.status());
            if (route.equals("tools")) { JsonObject result=new JsonObject(); result.add("tools",tools.get()); return get(method,result); }
            if(route.equals("resources")) {
                if(!method.equals("GET"))return method("GET");
                knownQuery(query,"source","kind","query","offset","limit");
                JsonObject fields=new JsonObject();
                for(String key:new String[]{"source","kind","query"})if(query.containsKey(key))fields.addProperty(key,query.get(key));
                fields.addProperty("offset",number(query,"offset",0));fields.addProperty("limit",number(query,"limit",24));
                return json(200,runtime.listManagedResources(fields));
            }
            if(route.equals("resources/rename") || route.equals("resources/delete")) {
                if(!method.equals("POST"))return method("POST");knownQuery(query);
                JsonObject fields=parse(body,4096);
                return json(200,route.endsWith("rename")?runtime.renameManagedResource(fields):runtime.deleteManagedResource(fields));
            }
            if(route.equals("resources/browse")) {
                if(!method.equals("GET"))return method("GET");knownQuery(query,"path","query","offset","limit");
                JsonObject fields=new JsonObject();for(String key:new String[]{"path","query"})if(query.containsKey(key))fields.addProperty(key,query.get(key));
                fields.addProperty("offset",number(query,"offset",0));fields.addProperty("limit",number(query,"limit",40));
                return json(200,runtime.browseDeviceFiles(fields));
            }
            if(route.equals("resources/import-file")) {
                if(!method.equals("POST"))return method("POST");knownQuery(query);return json(200,runtime.importDeviceFile(parse(body,8192)));
            }
            if(route.equals("resources/file-download")) {
                if(!method.equals("GET")&&!method.equals("HEAD"))return method("GET, HEAD");knownQuery(query,"path");
                if(!query.containsKey("path"))return error(400,"Choose an absolute phone file path");
                return runtime.downloadDeviceFile(query.get("path"),method,headers.get("range"));
            }
            if(route.startsWith("resources/artifact/")) {
                String[] segments=route.substring(19).split("/",-1);
                if(segments.length!=2 || !segments[1].equals("content"))return error(404,"Resource endpoint not found");
                if(!method.equals("GET")&&!method.equals("HEAD"))return method("GET, HEAD");knownQuery(query);
                io.github.libsumcp.protocol.ArtifactSource.Artifact artifact=runtime.openManagedResource("artifact",identifier(segments[0]));
                return FileHttpResponse.open(artifact.file,artifact.mimeType,artifact.fileName,method,headers.get("range"));
            }
            if(route.startsWith("media/")) {
                if(!method.equals("POST"))return method("POST");
                knownQuery(query);
                JsonObject payload=parse(body,route.equals("media/edit-image")||route.equals("media/video-edit/start")?256*1024:4096);
                switch(route) {
                    case "media/info":return json(200,runtime.attachmentMediaInfo(payload));
                    case "media/edit-image":return json(200,runtime.editImageAttachment(payload));
                    case "media/trim-video":return json(200,runtime.trimVideoAttachment(payload));
                    case "media/video-edit/start":return json(200,runtime.startVideoAttachmentEdit(payload));
                    case "media/video-edit/status":return json(200,runtime.videoAttachmentEditStatus(payload));
                    case "media/video-edit/cancel":return json(200,runtime.cancelVideoAttachmentEdit(payload));
                    case "media/video-storyboard/start":return json(200,runtime.startVideoStoryboard(payload));
                    case "media/video-storyboard/status":return json(200,runtime.videoStoryboardStatus(payload));
                    case "media/video-storyboard/cancel":return json(200,runtime.cancelVideoStoryboard(payload));
                    case "media/audio-waveform":return json(200,runtime.getAudioWaveform(payload));
                    case "media/audio-edit/start":return json(200,runtime.startAudioAttachmentEdit(payload));
                    case "media/audio-edit/status":return json(200,runtime.audioAttachmentEditStatus(payload));
                    case "media/audio-edit/cancel":return json(200,runtime.cancelAudioAttachmentEdit(payload));
                    default:return error(404,"Media editing endpoint not found");
                }
            }
            if(route.startsWith("capture/")) {
                if(!method.equals("POST"))return method("POST");
                knownQuery(query);
                JsonObject payload=parse(body,4096);
                switch(route) {
                    case "capture/screenshot":return json(200,runtime.captureScreenshot(payload));
                    case "capture/recording/start":return json(200,runtime.startCaptureRecording(payload));
                    case "capture/recording/status":return json(200,runtime.captureRecordingStatus(payload));
                    case "capture/recording/stop":return json(200,runtime.stopCaptureRecording(payload));
                    case "capture/import":
                        KnowledgeRuntime.keys(payload,"artifactId");
                        return json(200,runtime.importMediaArtifact(KnowledgeRuntime.string(payload,"artifactId",36)));
                    default:return error(404,"Capture endpoint not found");
                }
            }
            if(route.equals("attachments")) {
                if(!method.equals("GET")) return method("GET");
                knownQuery(query,"offset","limit","mediaType");
                return json(200,runtime.attachments().store().list(query.get("mediaType"),number(query,"offset",0),number(query,"limit",32)));
            }
            if(route.equals("attachments/upload")) return method("POST");
            if(route.startsWith("attachments/")) {
                String[] segments=route.substring(12).split("/",-1); String id=identifier(segments[0]);
                knownQuery(query);
                if(segments.length==1) {
                    if(method.equals("GET")) return json(200,runtime.attachments().store().read(id));
                    if(method.equals("DELETE")) return json(200,runtime.attachments().delete(id));
                    return method("GET, DELETE");
                }
                if(segments.length==2 && (segments[1].equals("content")||segments[1].equals("preview"))) {
                    if(!method.equals("GET") && !method.equals("HEAD")) return method("GET, HEAD");
                    boolean preview=segments[1].equals("preview");
                    JsonObject metadata=runtime.attachments().store().read(id);
                    File file=preview?runtime.attachments().store().previewFile(id):runtime.attachments().store().file(id);
                    if(file==null) return error(404,"Preview not found");
                    Response response=FileHttpResponse.open(file,preview?"image/jpeg":metadata.get("mimeType").getAsString(),preview?"preview.jpg":metadata.get("name").getAsString(),method,headers.get("range"));
                    return !preview&&Arrays.asList("file","archive").contains(metadata.get("kind").getAsString())?download(response):response;
                }
                return error(404,"Attachment endpoint not found");
            }
            if(route.equals("desktop/files") || route.equals("desktop/import")) {
                if(!method.equals("POST")) return method("POST");
                JsonObject payload=parse(body,256*1024);
                if(route.endsWith("files")) {
                    KnowledgeRuntime.keys(payload,"baseUrl","path","offset","limit");
                    return json(200,runtime.attachments().desktopFiles(KnowledgeRuntime.string(payload,"baseUrl",200),
                            payload.has("path")?optionalPath(payload):"",KnowledgeRuntime.integer(payload,"offset",0,100000,0),KnowledgeRuntime.integer(payload,"limit",1,100,50)));
                }
                KnowledgeRuntime.keys(payload,"baseUrl","path");
                return json(200,runtime.attachments().importDesktop(KnowledgeRuntime.string(payload,"baseUrl",200),optionalPath(payload)));
            }
            if (route.equals("documents")) {
                if (method.equals("GET")) {
                    knownQuery(query,"kind","query","offset","limit");
                    return json(200,runtime.documents().list(query.get("kind"),query.get("query"),number(query,"offset",0),number(query,"limit",50)));
                }
                if (method.equals("POST")) return json(200,runtime.saveDocument(parse(body,maxRequestBytes(target))));
                return method("GET, POST");
            }
            if (route.startsWith("documents/")) {
                String id=identifier(route.substring(10));
                if (method.equals("GET")) return json(200,runtime.documents().read(id));
                if (method.equals("DELETE")) return json(200,runtime.deleteDocument(id));
                return method("GET, DELETE");
            }
            if (route.startsWith("export/")) {
                if (!method.equals("GET")) return method("GET");
                String id=identifier(route.substring(7));
                byte[] markdown=runtime.documents().read(id).get("content").getAsString().getBytes(StandardCharsets.UTF_8);
                return new Response(200,"text/markdown; charset=utf-8",markdown,"Content-Disposition: attachment; filename=\""+id+".md\"\r\n");
            }
            if (route.equals("schedules")) {
                if (method.equals("GET")) return json(200,runtime.schedules().list());
                if (method.equals("POST")) return json(200,runtime.saveSchedule(parse(body,maxRequestBytes(target))));
                return method("GET, POST");
            }
            if (route.startsWith("schedules/")) {
                String id=identifier(route.substring(10));
                if (method.equals("GET")) return json(200,runtime.schedules().read(id));
                if (method.equals("DELETE")) return json(200,runtime.schedules().delete(id));
                return method("GET, DELETE");
            }
            if (route.equals("search")) {
                if (!method.equals("POST")) return method("POST");
                return json(200,runtime.searchDocuments(parse(body,maxRequestBytes(target))));
            }
            if (route.equals("vectors/import") || route.equals("vectors/search")) {
                if (!method.equals("POST")) return method("POST");
                JsonObject payload=parse(body,maxRequestBytes(target));
                return json(200,route.endsWith("import")?runtime.vectors().importVectors(payload):runtime.vectors().search(payload));
            }
            if (route.equals("vectors/status")) {
                if (!method.equals("GET")) return method("GET");
                knownQuery(query,"offset","limit");
                return json(200,runtime.vectors().status(number(query,"offset",0),number(query,"limit",100)));
            }
            if (route.startsWith("vectors/spaces/")) {
                if (!method.equals("DELETE")) return method("DELETE");
                String space=URLDecoder.decode(route.substring(15),StandardCharsets.UTF_8.name());
                return json(200,runtime.vectors().deleteSpace(space));
            }
            return error(404,"Endpoint not found");
        } catch (IllegalStateException conflict) { return error(409,KnowledgeRuntime.message(conflict)); }
        catch (IllegalArgumentException invalid) {
            String message=KnowledgeRuntime.message(invalid);
            return error(message.toLowerCase(Locale.ROOT).contains("not found")?404:400,message);
        } catch (Exception failed) {
            if(target.startsWith("/api/knowledge/spine/"))return error(400,KnowledgeRuntime.message(failed));
            if(target.startsWith("/api/knowledge/resources/"))return error(503,KnowledgeRuntime.message(failed));
            if(target.startsWith("/api/knowledge/capture/"))return error(503,KnowledgeRuntime.message(failed));
            if(target.startsWith("/api/knowledge/media/")) {
                String detail=KnowledgeRuntime.message(failed);
                detail=detail.replaceAll("[\\r\\n\\u0000-\\u001f]"," ");
                if(detail.length()>300)detail=detail.substring(0,300);
                return error(503,"媒体编辑未完成："+detail);
            }
            if(target.startsWith("/api/knowledge/desktop/")) return error(503,"Computer file service unavailable: "+KnowledgeRuntime.message(failed));
            return error(500,"Local storage request failed");
        }
    }
    private static Response get(String method,JsonObject value) { return method.equals("GET")?json(200,value):method("GET"); }
    private static boolean syncAttachmentPath(String path) {
        return path.matches("/api/knowledge/sync/attachments/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}");
    }
    private static String optionalPath(JsonObject value) {
        if(!value.has("path") || !value.get("path").isJsonPrimitive() || !value.getAsJsonPrimitive("path").isString() || value.get("path").getAsString().length()>1024)
            throw new IllegalArgumentException("path must be a relative file path");
        return value.get("path").getAsString();
    }
    private static Response method(String allowed) { return new Response(405,"application/json; charset=utf-8",
            "{\"error\":\"Method not allowed\"}".getBytes(StandardCharsets.UTF_8),"Allow: "+allowed+"\r\n"); }
    private static Response error(int status,String message) { JsonObject value=new JsonObject(); value.addProperty("error",message); return json(status,value); }
    private static Response json(int status,JsonObject value) { return new Response(status,"application/json; charset=utf-8",value.toString().getBytes(StandardCharsets.UTF_8)); }
    private static String identifier(String id) {
        if (!id.matches("[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")) throw new IllegalArgumentException("Invalid document or schedule id");
        return id;
    }
    private static Map<String,String> query(String text) throws IOException {
        Map<String,String> query=new LinkedHashMap<>();
        if (text.isEmpty()) return query;
        for (String item:text.split("&")) {
            String[] pair=item.split("=",2);
            String key=URLDecoder.decode(pair[0],StandardCharsets.UTF_8.name());
            String value=URLDecoder.decode(pair.length==2?pair[1]:"",StandardCharsets.UTF_8.name());
            if (query.put(key,value)!=null) throw new IllegalArgumentException("Duplicate query parameter");
        }
        return query;
    }
    private static void knownQuery(Map<String,String> query,String... names) {
        Set<String> allowed=new HashSet<>(Arrays.asList(names));
        for (String key:query.keySet()) if (!allowed.contains(key)) throw new IllegalArgumentException("Unknown query parameter: "+key);
    }
    private static int number(Map<String,String> query,String key,int fallback) {
        try { return query.containsKey(key)?Integer.parseInt(query.get(key)):fallback; }
        catch (NumberFormatException failed) { throw new IllegalArgumentException(key+" must be an integer"); }
    }
    private static JsonObject parse(byte[] body,int maximum) {
        if (body.length>maximum) throw new IllegalArgumentException("Request body exceeds limit");
        try {
            String text=StandardCharsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
                    .onUnmappableCharacter(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(body)).toString();
            int depth=0; boolean quoted=false,escaped=false;
            for (char ch:text.toCharArray()) {
                if (quoted) { if (escaped) escaped=false; else if(ch=='\\')escaped=true; else if(ch=='"')quoted=false; }
                else if(ch=='"')quoted=true;
                else if(ch=='{'||ch=='[') { if(++depth>32) throw new IOException("JSON nesting exceeds limit"); }
                else if(ch=='}'||ch==']')depth--;
            }
            JsonReader reader=new JsonReader(new StringReader(text)); reader.setStrictness(Strictness.STRICT);
            JsonElement result=JsonParser.parseReader(reader);
            if (!result.isJsonObject()||reader.peek()!=JsonToken.END_DOCUMENT) throw new IOException("Expected one JSON object");
            return result.getAsJsonObject();
        } catch (IOException|RuntimeException failed) { throw new IllegalArgumentException("Invalid JSON object"); }
    }
}
