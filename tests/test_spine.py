"""Generated resources and temporary stores only; no listener or personal files."""
import asyncio
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from urllib.parse import quote
import warnings
import zipfile

try:
    from desktop.knowledge import KnowledgeService, KnowledgeError
    from desktop.spine import SpineStore, _archive_limits, binary_version
except ImportError:
    from knowledge import KnowledgeService, KnowledgeError
    from spine import SpineStore, _archive_limits, binary_version


PNG = b"\x89PNG\r\n\x1a\n" + bytes(4) + b"IHDR" + bytes(16)
ATLAS = "纹理 1.png\nsize: 1,1\nfilter: Linear,Linear\npma: true\nregion\n  bounds: 0,0,1,1\n"


def skeleton(version="4.2.72"):
    return json.dumps({"skeleton": {"spine": version}, "bones": [{"name": "root"}], "animations": {}})


def archive(entries, compression=zipfile.ZIP_DEFLATED):
    output = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(output, "w", compression) as bundle:
            for name, content in entries:
                bundle.writestr(name, content)
    return output.getvalue()


class SpineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.service = KnowledgeService(Path(self.temporary.name) / "knowledge")

    def tearDown(self):
        self.service.close()
        self.temporary.cleanup()

    def upload(self, raw):
        return self.service.import_attachment(raw, "合成资源.zip", "application/zip", len(raw))

    def import_entries(self, entries):
        attachment = self.upload(archive(entries))
        return attachment, self.service.spine.import_bundle({"attachmentId": attachment["id"]})

    def test_versions_headers_unity_pairing_and_persistence(self):
        self.assertEqual(binary_version(bytes(8) + b"\x074.2.72"), "4.2.72")
        self.assertEqual(binary_version(b"\x05hash\x073.8.99"), "3.8.99")
        source, bundle = self.import_entries([
            ("深 层/角色-pro.skel.bytes", b"\x05hash\x073.8.99"),
            ("深 层/角色-pma.atlas.txt", ATLAS), ("深 层/纹理 1.png", PNG),
            ("旧版.json", skeleton("3.7.94")),
        ])
        binary = next(item for item in bundle["skeletons"] if item["format"] == "skel")
        self.assertEqual(binary["runtime"], "3.8")
        self.assertEqual(binary["atlas"], "深 层/角色-pma.atlas.txt")
        old = next(item for item in bundle["skeletons"] if item["format"] == "json")
        self.assertFalse(old["supported"])
        self.assertEqual(old["runtime"], "")
        self.assertIn("3.7.94", old["issue"])
        self.service.spine = SpineStore(self.service)
        self.assertEqual(self.service.spine.bundle(bundle["id"]), bundle)
        self.service.spine.delete(bundle["id"])
        self.assertEqual(self.service.read_attachment(source["id"])["kind"], "archive")
        self.assertFalse(self.service._media_pins)

    def test_ambiguous_atlases_require_selection_and_global_metadata_is_accepted(self):
        _, bundle = self.import_entries([
            ("角色.json.txt", skeleton()),
            ("one.atlas", "spine-version: 4.3.08\nhash: example\n\n" + ATLAS),
            ("two.atlas.txt", "spine-version: 4.3.08\n" + ATLAS), ("纹理 1.png", PNG),
        ])
        item = bundle["skeletons"][0]
        self.assertEqual(item["atlas"], "")
        self.assertEqual(len(item["atlasCandidates"]), 2)
        self.assertIn("手动选择", item["issue"])

    def test_only_valid_spine_and_image_files_are_served(self):
        _, bundle = self.import_entries([
            ("角色.json", skeleton()), ("角色.atlas", ATLAS), ("纹理 1.png", PNG),
            ("settings.json", '{"config":true}'), ("fake.png", b"<script>evil()</script>"),
            ("evil.html", b"<script>evil()</script>"), ("evil.js", b"evil()"),
        ])
        self.assertEqual(len(bundle["skeletons"]), 1)
        for name in ("settings.json", "fake.png", "evil.html", "evil.js", "../manifest.json"):
            with self.subTest(name=name), self.assertRaises(KnowledgeError):
                self.service.spine.file(bundle["id"], name)
        response = self.service.spine.file(bundle["id"], "角色.json")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    def test_unsafe_archive_paths_collisions_symlinks_and_bombs_clean_up(self):
        dangerous = [
            [("../escape.png", PNG)], [("/absolute.png", PNG)], [("C:/escape.png", PNG)],
            [("back\\slash.png", PNG)],
            [("same.png", PNG), ("same.png", PNG)],
            [("é.png", PNG), ("e\u0301.png", PNG)], [("A/one.png", PNG), ("a/two.png", PNG)],
            [("folder", b"file"), ("folder/child.png", PNG)], [("large.png", b"a" * (2 * 1024 * 1024))],
        ]
        link = zipfile.ZipInfo("link.png")
        link.create_system = 3
        link.external_attr = 0o120777 << 16
        dangerous.append([(link, "/etc/passwd")])
        for entries in dangerous:
            with self.subTest(entries=[str(item[0]) for item in entries]):
                uploaded = self.upload(archive([("allowed.json", skeleton()), *entries]))
                with self.assertRaises(KnowledgeError):
                    self.service.spine.import_bundle({"attachmentId": uploaded["id"]})
                self.assertFalse(list(self.service.spine.root.glob(".pending-*")))
                self.assertFalse(self.service._media_pins)
        # ZipFile's writer strips NUL itself; patch both same-length directory
        # and local names to exercise the untrusted reader's original name.
        raw = archive([("allowed.json", skeleton()), ("nullX.png", PNG)]).replace(b"nullX.png", b"null\x00.png")
        uploaded = self.upload(raw)
        with self.assertRaises(KnowledgeError):
            self.service.spine.import_bundle({"attachmentId": uploaded["id"]})
        self.assertFalse(list(self.service.spine.root.glob(".pending-*")))

    def test_forged_low_central_directory_count_is_rejected_before_allocation(self):
        raw = bytearray(archive([("one.json", "{}"), ("two.json", "{}")]))
        file = Path(self.temporary.name) / "central.zip"
        file.write_bytes(raw)
        _archive_limits(file)
        offset = raw.rfind(b"PK\x05\x06")
        struct.pack_into("<2H", raw, offset + 8, 1, 1)
        file.write_bytes(raw)
        with self.assertRaises(KnowledgeError):
            _archive_limits(file)

    def test_http_upload_device_routes_range_head_and_source_retention(self):
        from httpx import ASGITransport, AsyncClient
        from starlette.applications import Starlette
        from starlette.routing import Route
        try:
            from desktop.server import Desktop
        except ImportError:
            from server import Desktop

        desktop = Desktop.__new__(Desktop)
        desktop.knowledge = self.service

        async def local(request):
            return await desktop.local_api(request, request.url.path)

        app = Starlette(routes=[
            Route("/spine/{path:path}", desktop.local_api_spine, methods=["GET", "HEAD", "POST", "DELETE"]),
            Route("/api/knowledge/{path:path}", local, methods=["GET", "HEAD", "POST", "DELETE"]),
            Route("/device-api/{device}/{path:path}", desktop.device_api, methods=["GET", "HEAD", "POST", "DELETE"]),
        ])

        async def exercise():
            async with AsyncClient(transport=ASGITransport(app), base_url="http://generated-test") as client:
                uploaded = await client.post("/device-api/mac/api/knowledge/attachments/upload?name=test.zip",
                    content=archive([("空 格/角色.json", skeleton())]), headers={"Content-Type": "application/zip"})
                self.assertEqual(uploaded.status_code, 200)
                imported = await client.post("/spine/import", json={"attachmentId": uploaded.json()["id"]})
                self.assertEqual(imported.status_code, 200)
                bundle = imported.json()
                self.assertEqual(len((await client.get("/api/knowledge/spine/bundles")).json()["bundles"]), 1)
                url = "/device-api/mac/api/knowledge/spine/bundles/" + bundle["id"] + "/files/" + quote("空 格/角色.json", safe="/")
                full = await client.get(url)
                part = await client.get(url, headers={"Range": "bytes=0-9"})
                self.assertEqual(part.status_code, 206)
                self.assertEqual(part.content, full.content[:10])
                head = await client.head(url)
                self.assertEqual(head.content, b"")
                self.assertEqual(int(head.headers["Content-Length"]), len(full.content))
                self.assertEqual((await client.delete("/spine/bundles/" + bundle["id"])).status_code, 200)
                self.assertEqual((await client.get("/api/knowledge/attachments/" + uploaded.json()["id"])).status_code, 200)
                self.assertEqual((await client.post("/spine/import", content="{", headers={"Content-Type": "application/json"})).status_code, 400)
        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
