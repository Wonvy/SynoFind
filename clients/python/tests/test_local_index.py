import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from synofind_dsm.local_index import LocalIndex, PREFIX
from synofind_dsm.client import DSMError


@unittest.skipUnless(os.name == 'nt', 'Windows local file boundary')
class LocalIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'files'
        self.root.mkdir()
        (self.root / '项目 A').mkdir()
        (self.root / '项目 A' / '合同 Report 100%_预算.txt').write_text('测试内容', encoding='utf-8')
        (self.root / '另一份合同.pdf').write_bytes(b'%PDF-fixture')
        self.index = LocalIndex(self.base / 'state')
        self.sid = self.index.add(str(self.root))
        self.index.scan()

    def tearDown(self):
        self.index.close()
        self.temp.cleanup()

    def search(self, pattern='', **options):
        return self.index.search(dict(pattern=pattern, limit=50, offset=0, sort_by='name', **options))

    def test_short_chinese_multiple_terms_wildcards_literal_and_filters(self):
        self.assertEqual(self.search('合同')[1], 2)
        self.assertEqual(self.search('合同 REPORT')[1], 1)
        self.assertEqual(self.search('100%_')[1], 1)
        self.assertEqual(self.search('r?port')[1], 1)
        self.assertEqual(self.search('合同', extension='pdf')[1], 1)
        self.assertEqual(self.search('合同', min_size=1000)[1], 0)
        self.assertEqual(self.search('合同', modified_after=9999999999)[1], 0)

    def test_move_delete_add_and_remove_preserves_originals(self):
        (self.root / '另一份合同.pdf').rename(self.root / '新文件.pdf')
        (self.root / '项目 A' / '合同 Report 100%_预算.txt').unlink()
        self.index.scan()
        self.assertEqual(self.search('合同')[1], 0)
        self.assertEqual(self.search('新文件')[1], 1)
        self.index.remove(self.sid)
        self.assertEqual(self.search()[1], 0)
        self.assertTrue((self.root / '新文件.pdf').exists())

    def test_unreadable_subtree_keeps_records(self):
        original = os.scandir
        def fail(path):
            if Path(path).name == '项目 A':
                raise PermissionError()
            return original(path)
        with patch('synofind_dsm.local_index.os.scandir', side_effect=fail):
            self.index.scan()
        self.assertEqual(self.search('Report')[1], 1)
        self.assertEqual(self.index.sources()[0]['errors'], 1)

    def test_interrupted_scan_keeps_records_and_recovers(self):
        self.index.stop_event.set()
        self.index.scan()
        self.assertEqual(self.search('合同')[1], 2)
        self.index.stop_event.clear()
        self.index.scan()
        self.assertEqual(self.search('合同')[1], 2)

    def test_scoped_read_and_traversal_and_removed_source(self):
        file = self.search('Report')[0][0]
        with self.index.content(file['path']) as (_, response):
            self.assertEqual(response.read().decode(), '测试内容')
        for tail in ['/../secret.txt', '/x/../../secret.txt', '/secret.txt:stream']:
            with self.assertRaises(DSMError):
                self.index.info(PREFIX+self.sid+tail)
        self.index.remove(self.sid)
        with self.assertRaises(DSMError):
            self.index.info(file['path'])

    def test_persistent_index_and_duplicate_source(self):
        self.assertEqual(self.index.add(str(self.root)), self.sid)
        other = LocalIndex(self.base / 'state')
        try:
            self.assertEqual(other.search(dict(pattern='合同', limit=10, offset=0))[1], 2)
            self.assertEqual(len(other.sources()), 1)
        finally:
            other.close()

    def test_junctions_are_skipped(self):
        # A reparse point's attributes are rejected even when it resembles a directory.
        with patch('synofind_dsm.local_index.os.scandir') as scan:
            class Entry:
                name = 'escape'
                def stat(self, **kwargs):
                    from types import SimpleNamespace
                    return SimpleNamespace(st_mode=0o040755, st_file_attributes=0x400)
            scan.return_value.__enter__.return_value = iter([Entry()])
            self.index.scan()
        self.assertEqual(self.search('escape')[1], 0)


if __name__ == '__main__':
    unittest.main()
