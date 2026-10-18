package cn.ololee.devhelper.netdisk

import org.junit.Assert.*
import org.junit.Test

class NativeFileTransferToolsTest {
    @Test fun nativeToolsAreDiscoverableWithoutInitializingAnySdkOrAccount() {
        val all = BaiduNetdiskTools.list().map { it.asJsonObject }
        val names = all.map { it.get("name").asString }
        assertEquals(names.size, names.toSet().size)
        for (name in listOf("netdisk_local_roots", "netdisk_local_list", "netdisk_transfer_plan", "netdisk_transfer_execute",
            "netdisk_transfer_jobs", "netdisk_transfer_job", "netdisk_transfer_cancel")) {
            assertTrue(names.contains(name)); assertTrue(NativeFileTransferTools.handles(name))
        }
        assertFalse(NativeFileTransferTools.handles("netdisk_transfer_shell"))
    }

    @Test fun executionIsExplicitlyDestructiveAndReadOnlyDiscoveryCannotExecuteIt() {
        val tools = NativeFileTransferTools.list().map { it.asJsonObject }.associateBy { it.get("name").asString }
        val execute = tools.getValue("netdisk_transfer_execute")
        assertTrue(execute.getAsJsonObject("annotations").get("destructiveHint").asBoolean)
        assertFalse(execute.getAsJsonObject("annotations").get("readOnlyHint").asBoolean)
        assertEquals(setOf("planId", "planDigest"), execute.getAsJsonObject("inputSchema").getAsJsonArray("required").map { it.asString }.toSet())
        assertTrue(tools.getValue("netdisk_transfer_plan").getAsJsonObject("annotations").get("readOnlyHint").asBoolean)
        assertFalse(tools.getValue("netdisk_transfer_cancel").getAsJsonObject("annotations").get("readOnlyHint").asBoolean)
    }

    @Test fun transferSchemaContainsOnlyScopedReferencesAndBothDirections() {
        val schema = NativeFileTransferTools.list().map { it.asJsonObject }.first { it.get("name").asString == "netdisk_transfer_plan" }.getAsJsonObject("inputSchema")
        val fields = schema.getAsJsonObject("properties")
        assertFalse(schema.get("additionalProperties").asBoolean)
        assertEquals(setOf("local", "netdisk"), fields.getAsJsonObject("scope").getAsJsonArray("enum").map { it.asString }.toSet())
        assertEquals(setOf("copy", "move"), fields.getAsJsonObject("operation").getAsJsonArray("enum").map { it.asString }.toSet())
        val item = fields.getAsJsonObject("items").getAsJsonObject("items")
        assertFalse(item.get("additionalProperties").asBoolean)
        assertEquals(setOf("uri", "path", "fsId"), item.getAsJsonObject("properties").keySet())
        assertFalse(schema.toString().contains("base64", true))
        assertFalse(schema.toString().contains("accessToken"))
        assertFalse(schema.toString().contains("command"))
    }

    @Test fun mutationsHaveAReadOnlyPlanAndUseTheSameExplicitDestructiveExecutor() {
        val tools = BaiduNetdiskTools.list().map { it.asJsonObject }.associateBy { it.get("name").asString }
        val plan = tools.getValue("netdisk_file_operation_plan")
        val properties = plan.getAsJsonObject("inputSchema").getAsJsonObject("properties")
        assertEquals(setOf("rename", "trash"), properties.getAsJsonObject("operation").getAsJsonArray("enum").map { it.asString }.toSet())
        assertEquals(setOf("scope", "operation", "items", "name"), properties.keySet())
        assertTrue(plan.getAsJsonObject("annotations").get("readOnlyHint").asBoolean)
        assertFalse(plan.getAsJsonObject("annotations").get("destructiveHint").asBoolean)
        assertTrue(NativeFileTransferTools.handles("netdisk_file_operation_plan"))
    }

    @Test fun previewsUseScopedReferencesAndBinaryHttpInsteadOfEmbeddingImagesOrSecrets() {
        val all = BaiduNetdiskTools.list().map { it.asJsonObject }
        val names = all.map { it.get("name").asString }
        assertEquals(names.size, names.distinct().size)
        val tools = all.associateBy { it.get("name").asString }
        for (name in listOf("netdisk_file_preview_info", "netdisk_file_preview_open", "netdisk_file_preview_job", "netdisk_file_preview_close")) {
            assertTrue(NativeFilePreviewTools.handles(name))
            val schema = tools.getValue(name).getAsJsonObject("inputSchema")
            assertFalse(schema.get("additionalProperties").asBoolean)
            assertFalse(schema.toString().contains("base64", true))
            assertFalse(schema.toString().contains("accessToken"))
            assertFalse(schema.toString().contains("localPath"))
            assertFalse(tools.getValue(name).getAsJsonObject("annotations").get("destructiveHint").asBoolean)
        }
        assertEquals(setOf("scope", "uri", "path", "fsId"), tools.getValue("netdisk_file_preview_open").getAsJsonObject("inputSchema").getAsJsonObject("properties").keySet())
    }
}
