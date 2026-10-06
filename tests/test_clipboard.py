"""Clipboard regressions use owned fake data; they never open a system pasteboard."""
import unittest

try:
    from desktop.clipboard import ClipboardShare
except ImportError:
    from clipboard import ClipboardShare


class FakePreferences:
    def __init__(self):
        self.enabled = True

    def get(self):
        return {"autoClipboardSync": self.enabled}


class FakeClipboard:
    def __init__(self, text="initial", *, empty=False, omit_nontext=False):
        self.text = text
        self.empty = empty
        self.omit_nontext = omit_nontext
        self.counter = 0
        self.reads = 0
        self.writes = []
        self.read_error = None
        self.write_error = None
        self.read_override = None
        self.write_override = None

    def copy(self, text, *, empty=False):
        self.text, self.empty = text, empty
        self.counter += 1

    def read(self, max_chars=8192):
        self.reads += 1
        if self.read_error:
            raise self.read_error
        if self.read_override is not None:
            return dict(self.read_override)
        result = dict(status="ok", hasText=self.text is not None,
                      empty=self.empty if self.text is None else self.text == "",
                      truncated=self.text is not None and len(self.text) > max_chars,
                      length=len(self.text) if self.text is not None else 0,
                      changeCount=self.counter)
        if self.text is not None or not self.omit_nontext:
            result["text"] = self.text[:max_chars] if self.text is not None else ""
        return result

    def write(self, text):
        if self.write_error:
            raise self.write_error
        self.copy(text)
        self.writes.append(text)
        result = dict(status="ok", written=True, verified=True)
        if self.write_override:
            result.update(self.write_override)
        return result


class FakeAndroid(FakeClipboard):
    def __init__(self, text="initial", *, empty=False):
        super().__init__(text, empty=empty, omit_nontext=True)
        self.on_read = None

    async def __call__(self, name, args):
        if name == "research_get_clipboard":
            if self.on_read is not None:
                action, self.on_read = self.on_read, None
                action()
            return self.read(args["maxChars"])
        if name == "research_set_clipboard":
            return self.write(args["text"])
        raise AssertionError("Unexpected fake tool")


class ClipboardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mac = FakeClipboard("computer baseline")
        self.android = FakeAndroid("phone baseline")
        self.preferences = FakePreferences()
        self.share = ClipboardShare(self.preferences, self.android, local=self.mac)

    def assert_no_writes(self):
        self.assertEqual(self.mac.writes, [])
        self.assertEqual(self.android.writes, [])

    async def test_first_poll_only_establishes_baseline(self):
        await self.share.sync_once()
        self.assert_no_writes()
        self.assertEqual(self.share.current["version"], 0)
        self.assertEqual(set(self.share.baseline), {"mac", "android"})

    async def test_text_change_reaches_initially_empty_android_without_echo(self):
        self.android.copy(None, empty=True)
        await self.share.sync_once()
        self.mac.copy("合成文本\n😀")
        await self.share.sync_once()
        self.assertEqual(self.android.writes, ["合成文本\n😀"])
        self.assertEqual(self.mac.writes, [])
        version = self.share.current["version"]
        await self.share.sync_once()
        self.assertEqual(self.android.writes, ["合成文本\n😀"])
        self.assertEqual(self.mac.writes, [])
        self.assertEqual(self.share.current["version"], version)

    async def test_android_text_can_replace_unchanged_mac_image(self):
        self.mac.copy(None)
        await self.share.sync_once()
        self.android.copy("new phone text")
        await self.share.sync_once()
        self.assertEqual(self.mac.writes, ["new phone text"])
        self.assertEqual(self.android.writes, [])

    async def test_nontext_source_is_ignored_but_can_receive_future_text(self):
        await self.share.sync_once()
        self.mac.copy(None)
        await self.share.sync_once()
        self.assert_no_writes()
        self.android.copy("later text")
        await self.share.sync_once()
        self.assertEqual(self.mac.writes, ["later text"])

    async def test_explicit_empty_text_is_shared(self):
        self.android.copy(None)
        await self.share.sync_once()
        self.mac.copy("")
        await self.share.sync_once()
        self.assertEqual(self.android.writes, [""])
        self.assertTrue((await self.share.read_device("android"))["hasText"])

    async def test_both_changes_conflict_and_do_not_retry_overwriting(self):
        await self.share.sync_once()
        self.mac.copy("computer edit")
        self.android.copy("phone edit")
        await self.share.sync_once()
        self.assert_no_writes()
        self.assertIn("同时变化", self.share.view()["error"])
        await self.share.sync_once()
        self.assert_no_writes()
        result = await self.share.publish("chosen text", "both")
        self.assertIsNone(result["error"])
        await self.share.sync_once()
        self.assertEqual(self.mac.writes, ["chosen text"])
        self.assertEqual(self.android.writes, ["chosen text"])

    async def test_both_changes_to_equal_text_need_no_write(self):
        await self.share.sync_once()
        self.mac.copy("same text")
        self.android.copy("same text")
        await self.share.sync_once()
        self.assert_no_writes()
        self.assertEqual(self.share.current["text"], "same text")

    async def test_concurrent_text_and_image_changes_preserve_both(self):
        await self.share.sync_once()
        self.mac.copy("new text")
        self.android.copy(None)
        await self.share.sync_once()
        self.assert_no_writes()
        self.assertIn("同时变化", self.share.view()["error"])

    async def test_destination_change_during_network_read_is_not_overwritten(self):
        await self.share.sync_once()
        self.android.copy("phone edit")
        self.android.on_read = lambda: self.mac.copy("computer copy during phone read")
        await self.share.sync_once()
        self.assert_no_writes()
        self.assertEqual(self.mac.text, "computer copy during phone read")
        self.assertIn("同步期间变化", self.share.current["error"])

    async def test_auto_sharing_refuses_truncated_samples(self):
        await self.share.sync_once()
        self.mac.copy("x" * 8193)
        with self.assertRaisesRegex(RuntimeError, "超过"):
            await self.share.sync_once()
        self.assert_no_writes()

    async def test_pull_can_preview_truncation_but_cannot_transfer_it(self):
        self.mac.copy("x" * 8193)
        result = await self.share.pull("mac")
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["text"]), 8192)
        self.assertNotIn("mac", self.share.baseline)
        with self.assertRaisesRegex(RuntimeError, "超过"):
            await self.share.pull("mac", "android")
        self.assert_no_writes()

    async def test_android_nontext_without_text_field_is_readable_but_not_transferred(self):
        self.android.copy(None)
        result = await self.share.pull("android")
        self.assertFalse(result["hasText"])
        self.assertEqual(result["text"], "")
        with self.assertRaisesRegex(RuntimeError, "不是文本"):
            await self.share.pull("android", "mac")
        self.assert_no_writes()

    async def test_invalid_targets_are_rejected_before_io(self):
        for invalid in ("", "other", "MAC", None, 1):
            with self.assertRaises(ValueError):
                await self.share.write_device(invalid, "text")
            with self.assertRaises(ValueError):
                await self.share.publish("text", invalid)
            if invalid is not None:
                with self.assertRaises(ValueError):
                    await self.share.pull("mac", invalid)
        with self.assertRaises(ValueError):
            await self.share.pull("other", "android")
        self.assertEqual(self.mac.reads, 0)
        self.assertEqual(self.android.reads, 0)
        self.assert_no_writes()

    async def test_publish_reports_partial_write_without_false_adoption(self):
        self.mac.write_error = OSError("fake Mac failure")
        result = await self.share.publish("owned test", "both")
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["partial"])
        self.assertEqual(result["writtenDevices"], ["android"])
        self.assertEqual(result["failedDevice"], "mac")
        self.assertIn("fake Mac failure", result["error"])
        self.assertEqual(self.share.current["version"], 0)
        self.assertEqual(self.android.text, "owned test")
        self.assertEqual(self.mac.text, "computer baseline")

    async def test_publish_first_write_failure_does_not_touch_other_target(self):
        self.android.write_error = RuntimeError("fake phone failure")
        result = await self.share.publish("owned test", "both")
        self.assertEqual(result["status"], "error")
        self.assertFalse(result["partial"])
        self.assertEqual(result["writtenDevices"], [])
        self.assertEqual(result["failedDevice"], "android")
        self.assert_no_writes()

    async def test_pull_reports_target_failure(self):
        self.mac.write_error = OSError("fake Mac failure")
        result = await self.share.pull("android", "both")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["writtenDevices"], [])
        self.assertEqual(result["failedDevice"], "mac")
        self.assertEqual(self.share.current["version"], 0)
        self.assert_no_writes()

    async def test_foreground_required_is_not_an_empty_clipboard(self):
        self.android.read_override = dict(status="foregroundRequired", foregroundRequired=True,
                                          error="fake foreground required")
        with self.assertRaisesRegex(RuntimeError, "fake foreground required"):
            await self.share.sync_once()
        self.assertEqual(self.share.baseline, {})
        self.assert_no_writes()

    async def test_unverified_write_is_reported_as_unconfirmed(self):
        self.android.write_override = {"verified": False}
        result = await self.share.publish("owned test", "android")
        self.assertEqual(result["status"], "error")
        self.assertIn("无法确认", result["error"])
        self.assertEqual(self.share.current["version"], 0)
        self.assertNotIn("android", self.share.baseline)

    async def test_unconfirmed_manual_write_is_not_echoed_to_other_device(self):
        await self.share.sync_once()
        self.android.write_override = {"verified": False}
        result = await self.share.publish("owned uncertain write", "android")
        self.assertEqual(result["status"], "error")
        await self.share.sync_once()
        self.assertEqual(self.mac.writes, [])
        self.assertEqual(self.mac.text, "computer baseline")

    async def test_failed_sync_can_retry_after_target_recovers(self):
        await self.share.sync_once()
        self.mac.copy("retry text")
        self.android.write_error = OSError("fake offline")
        with self.assertRaisesRegex(OSError, "fake offline"):
            await self.share.sync_once()
        self.android.write_error = None
        await self.share.sync_once()
        await self.share.sync_once()
        self.assertEqual(self.android.writes, ["retry text"])
        self.assertEqual(self.mac.writes, [])

    async def test_invalid_text_response_never_clears_target(self):
        self.android.read_override = dict(status="ok", hasText=True)
        with self.assertRaisesRegex(RuntimeError, "有效"):
            await self.share.pull("android", "mac")
        self.assert_no_writes()

    async def test_invalid_text_is_rejected_before_writing_either_device(self):
        for value in (None, 12, "nul\0text", "x" * 8193, "😀" * 4097, "\ud800"):
            with self.assertRaises(ValueError):
                await self.share.publish(value, "both")
        self.assert_no_writes()

    async def test_partial_baseline_after_manual_write_does_not_trigger_initial_sync(self):
        await self.share.publish("manual phone text", "android")
        await self.share.sync_once()
        self.assertEqual(self.android.writes, ["manual phone text"])
        self.assertEqual(self.mac.writes, [])

    async def test_manual_full_baseline_is_not_the_first_auto_sync_baseline(self):
        await self.share.publish("manual initial text", "both")
        self.mac.copy("copy made before automatic sharing")
        await self.share.sync_once()
        self.assertEqual(self.android.text, "manual initial text")
        self.assertEqual(self.android.writes, ["manual initial text"])

    async def test_disabling_during_a_poll_does_not_write_and_reenable_sets_baseline(self):
        await self.share.sync_once()
        self.mac.copy("old changed text")
        self.preferences.enabled = False
        await self.share.sync_once()
        self.assert_no_writes()
        self.preferences.enabled = True
        await self.share.sync_once()
        self.assert_no_writes()
        self.mac.copy("new copy after enabling")
        await self.share.sync_once()
        self.assertEqual(self.android.writes, ["new copy after enabling"])

    async def test_invalid_read_limit_does_not_access_a_device(self):
        for value in (0, 8193, True, "10"):
            with self.assertRaises(ValueError):
                await self.share.read_device("mac", value)
        self.assertEqual(self.mac.reads, 0)


if __name__ == "__main__":
    unittest.main()
