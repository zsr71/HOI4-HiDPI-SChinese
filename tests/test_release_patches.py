import unittest

from schinese.apply_release_patches import reconstruct, sha256


class ReleasePatchTests(unittest.TestCase):
    def test_preserves_bom_crlf_and_non_ascii(self):
        source = '\ufeffa\r\n原文\r\nc\r\n'.encode('utf-8')
        target = '\ufeffa\r\n新文\r\nc\r\n'.encode('utf-8')
        entry = {'path': 'interface/example.gui', 'source_sha256': sha256(source),
                 'target_sha256': sha256(target),
                 'ops': [{'copy_lines': [0, 1]}, {'text': '新文\r\n'}, {'copy_lines': [2, 3]}]}
        self.assertEqual(reconstruct(entry, source), target)
        with self.assertRaises(ValueError):
            reconstruct(entry, source + b'changed')

    def test_corrupt_patch_rejected(self):
        entry = {'path': 'test', 'source_sha256': None,
                 'target_sha256': sha256(b'expected'), 'ops': [{'text': 'wrong'}]}
        with self.assertRaises(ValueError):
            reconstruct(entry, b'')
