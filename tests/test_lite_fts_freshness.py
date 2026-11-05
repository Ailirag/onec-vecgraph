import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from onec_vecgraph.lite.fts import FtsIndex
from onec_vecgraph.lite.workspace import Workspace
from onec_vecgraph.lite import freshness


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root/'src/demo'
        self.repo.mkdir(parents=True)
        self.locks = self.root/'home/.onec-lite/refresh-locks'
        self.locks.mkdir(parents=True)
        self.env = patch.dict(os.environ, {'HOME': str(self.root/'home'), 'ONEC_LITE_REQUIRE_FRESH_INDEX': '1',
                                          'ONEC_LITE_REFRESH_LOCKS': str(self.locks)})
        self.env.start()
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('remote', 'add', 'origin', 'https://git.example.invalid/test/demo.git')
        (self.repo/'Configuration.xml').write_text('''<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"><Configuration uuid="00000000-0000-0000-0000-000000000001"><Properties><Name>Demo</Name></Properties></Configuration></MetaDataObject>''')
        self.module = self.repo/'CommonModules/Demo/Ext/Module.bsl'
        self.module.parent.mkdir(parents=True)
        self.module.write_text('Процедура Alpha() Экспорт\n Сообщить("OldMarker");\nКонецПроцедуры\n', encoding='utf-8')
        self.commit()
        self.ws = Workspace(self.repo)
        self.index = FtsIndex(self.ws)

    def tearDown(self):
        self.env.stop(); self.tmp.cleanup()

    def git(self, *args):
        return subprocess.check_output(['git','-c','safe.directory=*','-C',str(self.repo),*args],stderr=subprocess.DEVNULL,text=True).strip()

    def commit(self):
        self.git('add','.'); self.git('commit','-qm','fixture')

    def build(self):
        # Explicit command with no mirror publication underway.
        result = self.index.build(wait=1)
        self.assertNotIn('error', result)
        self.assertTrue(self.index.freshness()['fresh'])

    def test_current_index_and_commit_identity(self):
        self.build()
        result = self.index.search('OldMarker')
        self.assertTrue(result['ready'])
        self.assertTrue(result['freshness']['fresh'])
        self.assertGreater(result['total_matches'], 0)

    def test_old_date_with_same_commit_is_current(self):
        self.build()
        import sqlite3
        with sqlite3.connect(self.index.path) as db: db.execute("UPDATE meta SET value='2000-01-01 00:00:00' WHERE key='built_at'")
        self.assertTrue(self.index.freshness()['fresh'])

    def test_new_commit_cannot_return_old_search_results(self):
        self.build()
        self.module.write_text('Процедура Beta() Экспорт\n Сообщить("NewMarker");\nКонецПроцедуры\n',encoding='utf-8')
        self.commit()
        with patch.object(self.index, 'ensure_background'):
            result = self.index.search('OldMarker')
        self.assertFalse(result['ready']); self.assertIn('error',result)
        self.assertNotIn('results',result)
        self.assertNotEqual(result['freshness']['source_heads'],result['freshness']['indexed_heads'])
        self.build()
        self.assertGreater(self.index.search('NewMarker')['total_matches'],0)
        self.assertEqual(self.index.search('OldMarker')['total_matches'],0)

    def test_deleted_file_is_removed_on_incremental_refresh(self):
        self.build(); self.module.unlink(); self.commit(); self.build()
        self.assertEqual(self.index.search('OldMarker')['total_matches'],0)

    def test_writer_lock_returns_updating_without_search_or_lazy_build(self):
        self.build()
        with freshness.write_lock(self.ws.root) as held:
            self.assertTrue(held)
            with patch.object(self.index,'_search_current',side_effect=AssertionError('must not read')):
                result=self.index.search('OldMarker')
            self.assertEqual(result['freshness']['state'],'updating')
            self.assertEqual(self.index.status()['freshness']['state'],'updating')
            self.assertEqual(self.index.build(wait=0)['status'],'building')

    def test_source_head_drift_during_read_discards_result(self):
        self.build()
        original=self.index._search_current
        def read(*args,**kwargs):
            result=original(*args,**kwargs)
            self.module.write_text('Процедура Gamma() Экспорт\nКонецПроцедуры\n',encoding='utf-8');self.commit()
            return result
        with patch.object(self.index,'_search_current',side_effect=read):
            result=self.index.search('OldMarker')
        self.assertFalse(result['ready']);self.assertNotIn('results',result)

    def test_legacy_mode_keeps_existing_contract(self):
        self.build()
        with patch.dict(os.environ,{'ONEC_LITE_REQUIRE_FRESH_INDEX':'0'}):
            result=self.index.search('OldMarker')
        self.assertTrue(result['ready']);self.assertNotIn('freshness',result)

    def test_reader_does_not_block_and_writer_excludes_readers(self):
        with freshness.read_lock(self.ws.root) as a, freshness.read_lock(self.ws.root) as b:
            self.assertTrue(a and b)  # читатели не мешают друг другу
            with freshness.write_lock(self.ws.root, block=False) as w:
                self.assertFalse(w)   # рефреш не публикует файлы под читающим поиском
        with freshness.write_lock(self.ws.root, block=False) as w:
            self.assertTrue(w)
            with freshness.read_lock(self.ws.root) as r:
                self.assertFalse(r)


if __name__=='__main__':unittest.main(verbosity=2)
