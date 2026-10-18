import unittest
from file_name_patterns import renamed_name


class RenamePatternTests(unittest.TestCase):
    def test_literal_single_and_batch_tokens_preserve_extensions(self):
        self.assertEqual(renamed_name('new.lua','old.lua',False,1,1),'new.lua')
        self.assertEqual(renamed_name('asset_{index}{ext}','old.diffuse.png',False,2,3),'asset_2.png')
        self.assertEqual(renamed_name('{name}_{index}{ext}','old.diffuse.png',False,1,3),'old.diffuse_1.png')
        self.assertEqual(renamed_name('{name}_{index}{ext}','folder.name',True,1,2),'folder.name_1')
        self.assertEqual(renamed_name('{name}_{index}{ext}','.gitignore',False,1,2),'.gitignore_1')

    def test_unknown_tokens_paths_and_invalid_expanded_names_are_rejected(self):
        for template in ('one-name.txt','{index:03}','{other}','../{name}','{name}/x','{name}\x85','{name}.'):
            with self.subTest(template=template),self.assertRaises(ValueError):
                renamed_name(template,'old.lua',False,1,2)
        with self.assertRaises(ValueError):
            renamed_name('{name}_suffix','a'*240,False,1,1)
