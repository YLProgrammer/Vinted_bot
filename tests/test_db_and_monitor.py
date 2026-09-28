import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("CENTRAL_BOT_TOKEN", "test")
os.environ.setdefault("SENDER_BOT_TOKEN", "test")
os.environ.setdefault("DEV_BOT_TOKEN", "test")
os.environ.setdefault("DEV_CHAT_ID", "1")


class DatabaseAndMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "test.db")
        os.environ["DB_PATH"] = self.db_path
        # The config module is loaded afresh so every test owns its database.
        for name in ("config", "db", "monitor_service"):
            sys.modules.pop(name, None)
        import db
        import monitor_service
        self.db = db
        self.monitor_service = monitor_service
        self.db.init_db()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_initialization_records_schema_version_and_is_idempotent(self):
        self.assertEqual(self.db.get_schema_version(), 1)
        self.db.init_db()
        with self.db.get_connection() as conn:
            versions = conn.execute("SELECT version FROM schema_migrations").fetchall()
            indexes = {row[1] for row in conn.execute("PRAGMA index_list(searches)").fetchall()}
        self.assertEqual(versions, [(1,)])
        self.assertIn("idx_searches_active", indexes)

    def test_legacy_database_is_upgraded_without_losing_user(self):
        import sqlite3
        connection = sqlite3.connect(self.db_path)
        connection.execute("DROP TABLE users")
        connection.execute("CREATE TABLE users (chat_id TEXT PRIMARY KEY, created_at TIMESTAMP)")
        connection.execute("INSERT INTO users (chat_id) VALUES ('legacy-user')")
        connection.commit()
        connection.close()
        self.db.init_db()
        user = self.db.get_user("legacy-user")
        self.assertEqual(user["language"], "fr")
        self.assertEqual(self.db.get_schema_version(), 1)

    def test_duplicate_and_price_drop_filtering(self):
        item = {"titre": "Nike TN", "prix": "100,00 €", "lien": "https://item/1", "image": None}
        first, drops = self.monitor_service.filter_duplicates_and_price_drops("42", [item])
        self.assertEqual(first, [item])
        self.assertEqual(drops, [])
        cheaper = {**item, "prix": "75 €"}
        second, drops = self.monitor_service.filter_duplicates_and_price_drops("42", [cheaper])
        self.assertEqual(second, [])
        self.assertEqual(drops[0]["ancien_prix"], 100.0)
        self.assertEqual(drops[0]["nouveau_prix"], "75 €")


if __name__ == "__main__":
    unittest.main()
