"""Android integration publication must remain an explicit, source-only list."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


helper_path = Path(__file__).resolve().parents[1] / 'scripts/android_native_sources.py'
spec = importlib.util.spec_from_file_location('android_native_release_test', helper_path)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class AndroidNativeSourceTests(unittest.TestCase):
    def fixture(self, root):
        for relative in helper.ANDROID_FILES:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('// generated public source fixture\n')

    def test_development_and_published_snapshots_have_identical_source_boundaries(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            development = base / 'development'
            source = development / 'desktop'
            source.mkdir(parents=True)
            self.fixture(development)
            first = helper.release_files(source)
            public = base / 'public'
            self.fixture(public / 'android-native')
            second = helper.release_files(public)
            self.assertEqual(set(first), set(second))
            self.assertEqual(len(first), len(helper.ANDROID_FILES))
            self.assertTrue(all(first[key].read_bytes() == second[key].read_bytes() for key in first))
            self.assertTrue(all(path.startswith('android-native/') for path in first))
            self.assertFalse(any(path.endswith(('.aar', '.apk', '.so', '.json')) for path in first))

    def test_missing_sources_and_symlinked_components_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / 'desktop'
            source.mkdir()
            self.fixture(base)
            target = base / helper.ANDROID_FILES[0]
            content = target.read_text()
            target.unlink()
            with self.assertRaisesRegex(ValueError, 'missing'):
                helper.release_files(source)
            outside = base / 'generated-outside.kt'
            outside.write_text(content)
            target.symlink_to(outside)
            with self.assertRaisesRegex(ValueError, 'symlink'):
                helper.release_files(source)

    def test_binaries_private_configuration_and_traversal_cannot_extend_the_list(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / 'desktop'
            source.mkdir()
            self.fixture(base)
            for unsafe in ('app/libs/generated.aar', 'app/google-services.json', '../generated.kt'):
                with self.subTest(path=unsafe), patch.object(helper, 'ANDROID_FILES', helper.ANDROID_FILES + (unsafe,)):
                    with self.assertRaises(ValueError):
                        helper.release_files(source)
