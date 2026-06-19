from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from agent_engine.state_store import control_plane_path, migrate_legacy_tables


class StateStoreTests(unittest.TestCase):
    def test_legacy_tables_migrate_once_into_control_plane_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = control_plane_path(root)
            legacy = root / "legacy.sqlite"

            target_conn = sqlite3.connect(target)
            target_conn.execute("CREATE TABLE records (id TEXT PRIMARY KEY, value TEXT NOT NULL)")
            target_conn.commit()
            target_conn.close()

            legacy_conn = sqlite3.connect(legacy)
            legacy_conn.execute("CREATE TABLE records (id TEXT PRIMARY KEY, value TEXT NOT NULL)")
            legacy_conn.execute("INSERT INTO records (id, value) VALUES ('one', 'legacy')")
            legacy_conn.commit()
            legacy_conn.close()

            self.assertTrue(migrate_legacy_tables(target, legacy, ("records",)))
            self.assertFalse(migrate_legacy_tables(target, legacy, ("records",)))

            conn = sqlite3.connect(target)
            try:
                rows = conn.execute("SELECT id, value FROM records").fetchall()
                migrations = conn.execute("SELECT key FROM state_migrations").fetchall()
            finally:
                conn.close()

            self.assertEqual(rows, [("one", "legacy")])
            self.assertEqual(migrations, [("legacy:legacy.sqlite",)])

    def test_concurrent_legacy_migration_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = control_plane_path(root)
            legacy = root / "legacy.sqlite"

            for path in (target, legacy):
                conn = sqlite3.connect(path)
                conn.execute("CREATE TABLE records (id TEXT PRIMARY KEY, value TEXT NOT NULL)")
                if path == legacy:
                    conn.execute("INSERT INTO records (id, value) VALUES ('one', 'legacy')")
                conn.commit()
                conn.close()

            barrier = threading.Barrier(2)
            results: list[bool] = []
            errors: list[Exception] = []

            def migrate() -> None:
                try:
                    barrier.wait(timeout=5)
                    results.append(migrate_legacy_tables(target, legacy, ("records",)))
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=migrate), threading.Thread(target=migrate)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

            self.assertEqual(errors, [])
            self.assertEqual(sorted(results), [False, True])


if __name__ == "__main__":
    unittest.main()
