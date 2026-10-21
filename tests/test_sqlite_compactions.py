"""Synthetic canonical-store coverage; no live sessions or credentials."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_session_footer import FOOTER_PATH, args_for, footer


class SQLiteCompactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / '.openclaw/agents/main/agent/openclaw-agent.sqlite'
        self.path.parent.mkdir(parents=True)
        self.key = 'agent:test:synthetic'
        self.payload = '{"synthetic":"測試"}'
        with sqlite3.connect(self.path) as db:
            db.executescript('''
                CREATE TABLE session_nodes(session_key TEXT PRIMARY KEY, current_session_id TEXT, entry_valid INTEGER, entry_json TEXT);
                CREATE TABLE session_windows(session_id TEXT PRIMARY KEY, session_key TEXT);
                CREATE TABLE transcript_events(session_id TEXT, event_json TEXT, event_zstd BLOB, event_utf8_bytes INTEGER);
                CREATE TABLE session_transcript_cold_archives(session_id TEXT);
            ''')
            entry = {'compactionCount': 3, 'totalTokens': 999999, 'contextTokens': 888888,
                     'unrelatedPrivateMetadata': 'must-not-be-returned'}
            db.execute('INSERT INTO session_nodes VALUES(?,?,1,?)', (self.key, 'current', json.dumps(entry)))
            db.executemany('INSERT INTO session_windows VALUES(?,?)', [('current', self.key), ('old', self.key)])
            db.execute('INSERT INTO transcript_events VALUES(?,?,NULL,NULL)', ('current', self.payload))
            db.execute('INSERT INTO transcript_events VALUES(?,NULL,?,?)', ('current', b'opaque-compressed', 41))
            db.execute('INSERT INTO transcript_events VALUES(?,?,NULL,NULL)', ('old', 'old generation must not be counted'))

    def args(self, **values):
        return args_for(self.root, sessions_db=str(self.path), sessions_json=None,
                        session_key=self.key, status_text='📚 Context: 144K/272K', **values)

    def set_entry(self, entry):
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE session_nodes SET entry_json=? WHERE session_key=?', (json.dumps(entry), self.key))

    def test_exact_generation_count_and_logical_bytes_without_private_fields(self):
        before = hashlib.sha256(self.path.read_bytes()).digest()
        result = footer.build_payload(self.args())
        self.assertEqual(result['compactions'], 3)
        self.assertEqual(result['compactionSource'], 'sqlite-session-entry')
        self.assertIn('🧹 對話壓縮：3 次', result['lines'])
        self.assertEqual(result['sizeBytes'], len(self.payload.encode()) + 41)
        self.assertEqual(result['contextTokens'], 144000)
        self.assertEqual(result['contextLimit'], 272000)
        self.assertEqual(result['transcriptSource'], 'sqlite-event-payload-bytes')
        self.assertNotIn('must-not-be-returned', json.dumps(result))
        self.assertNotIn('888888', json.dumps(result))
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).digest(), before)

    def test_fresh_runtime_count_overrides_store(self):
        args = self.args()
        args.status_text += ' · 🧹 Compactions: 4'
        result = footer.build_payload(args)
        self.assertEqual((result['compactions'], result['compactionSource']), (4, 'runtime-status'))

    def test_zero_and_missing_count_remain_visible(self):
        for entry in ({'compactionCount': 0}, {}):
            with self.subTest(entry=entry):
                self.set_entry(entry)
                result = footer.build_payload(self.args(no_history=True))
                self.assertEqual(result['compactions'], 0)
                self.assertIn('🧹 對話壓縮：0 次', result['lines'])

    def test_invalid_count_is_unknown_not_fabricated_zero(self):
        for value in (-1, True, 'not-a-count', 1.5):
            with self.subTest(value=value):
                self.set_entry({'compactionCount': value})
                result = footer.build_payload(self.args(no_history=True))
                self.assertIsNone(result['compactions'])
                self.assertIn('unknown', result['compactionLine'])

    def test_generation_change_resets_count_and_high_water(self):
        footer.build_payload(self.args())
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO session_windows VALUES(?,?)', ('new', self.key))
            db.execute('UPDATE session_nodes SET current_session_id=?,entry_json=? WHERE session_key=?', ('new', '{"compactionCount":0}', self.key))
            db.execute('INSERT INTO transcript_events VALUES(?,?,NULL,NULL)', ('new', '{}'))
        args = self.args()
        args.status_text = '📚 Context: 20K/272K'
        result = footer.build_payload(args)
        self.assertEqual(result['compactions'], 0)
        self.assertEqual(result['contextTokens'], 20000)

    def test_missing_key_and_nonexact_selector_fail_closed(self):
        with self.assertRaises(KeyError):
            footer.read_sqlite_session(str(self.path), 'absent', False)
        with self.assertRaisesRegex(ValueError, 'exact --session-key'):
            footer.read_sqlite_session(str(self.path), None, False)

    def test_unknown_transcript_does_not_hide_count(self):
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO session_transcript_cold_archives VALUES(?)', ('current',))
        result = footer.build_payload(self.args(strict=False))
        self.assertIsNone(result['sizeBytes'])
        self.assertEqual(result['compactions'], 3)

    def test_canonical_default_and_explicit_json_override(self):
        legacy = self.root / 'legacy.json'
        legacy.write_text(json.dumps({self.key: {'compactionCount': 9}}))
        common = [sys.executable, str(FOOTER_PATH), '--session-key', self.key, '--no-history', '--json', '--status-text', '📚 Context: 12K/272K']
        env = dict(os.environ, HOME=str(self.root))
        canonical = subprocess.run(common, env=env, text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(canonical.stdout)['compactions'], 3)
        offline = subprocess.run(common + ['--sessions-json', str(legacy)], env=env, text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(offline.stdout)['compactions'], 9)
        self.path.write_bytes(b'invalid SQLite')
        broken = subprocess.run(common, env=env, text=True, capture_output=True)
        self.assertNotEqual(broken.returncode, 0)
        self.assertNotIn('9 次', broken.stdout)

    def test_connection_is_readonly_and_metadata_is_projected(self):
        connect = sqlite3.connect
        statements = []
        connections = []
        def audited(database_uri, **kwargs):
            self.assertTrue(database_uri.endswith('?mode=ro'))
            db = connect(database_uri, **kwargs)
            db.set_trace_callback(statements.append)
            connections.append(db)
            return db
        with patch.object(footer.sqlite3, 'connect', side_effect=audited):
            footer.read_sqlite_session(str(self.path), self.key, True)
        self.assertEqual(statements[0], 'PRAGMA query_only=ON')
        self.assertTrue(any("json_extract(n.entry_json, '$.compactionCount')" in s for s in statements))
        self.assertFalse(any('SELECT n.entry_json ' in s for s in statements))
        for db in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                db.execute('SELECT 1')


if __name__ == '__main__':
    unittest.main()
