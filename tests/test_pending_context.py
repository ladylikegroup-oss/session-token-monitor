"""Regression: missing post-compaction samples must not become stale usage."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

CORE = Path(__file__).resolve().parents[1] / "skills/session-token-monitor/scripts/session_footer.py"

class PendingContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = self.root / "sessions.json"
        self.store.write_text(json.dumps({"agent:test:pending": {"sessionId": "synthetic", "compactionCount": 0}}))

    def run_sample(self, status):
        result = subprocess.run([sys.executable, str(CORE), "--sessions-json", str(self.store),
            "--session-key", "agent:test:pending", "--history-json", str(self.root / "history.json"),
            "--status-text", status, "--json"], capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def test_unknown_usage_retains_fresh_limit(self):
        for unknown in ("?", "unknown"):
            with self.subTest(unknown=unknown):
                result = self.run_sample(f"📚 Context: {unknown}/272k\n🧹 Compactions: 1")
                self.assertIsNone(result["contextTokens"])
                self.assertEqual(result["contextLimit"], 272000)
                self.assertEqual(result["contextLine"], "📊 累積 Context：待回報 / 272K")
                self.assertEqual(result["compactions"], 1)

    def test_pending_does_not_reuse_old_high_water_and_numeric_sample_recovers(self):
        old = self.run_sample("📚 Context: 345k/272k\n🧹 Compactions: 0")
        self.assertEqual(old["contextTokens"], 345000)
        pending = self.run_sample("📚 Context: ?/272k\n🧹 Compactions: 1")
        self.assertIsNone(pending["contextTokens"])
        fresh = self.run_sample("📚 Context: 60k/272k\n🧹 Compactions: 1")
        self.assertEqual(fresh["contextLine"], "📊 累積 Context：60K / 272K")
        lower = self.run_sample("📚 Context: 55k/272k\n🧹 Compactions: 1")
        self.assertEqual(lower["contextTokens"], 60000)

    def test_no_usage_or_limit_stays_pending_without_fabrication(self):
        result = self.run_sample("🧹 Compactions: 1")
        self.assertIsNone(result["contextTokens"])
        self.assertIsNone(result["contextLimit"])
        self.assertEqual(result["contextLine"], "📊 累積 Context：待回報")

if __name__ == "__main__":
    unittest.main()
