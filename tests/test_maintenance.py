"""Check the two operational paths added for the small deployment."""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import app as viewer
from deploy import backup_data


class MaintenanceTest(unittest.TestCase):
    def test_official_group_rule_preserves_default_and_accepts_ids(self):
        with patch.dict(viewer.os.environ, {}, clear=False):
            viewer.os.environ.pop("VIEWER_OFFICIAL_GROUP_KEYWORDS", None)
            viewer.os.environ.pop("VIEWER_OFFICIAL_GROUP_IDS", None)
            condition, params = viewer.official_group_rule()
            self.assertEqual(params, (["%官群%"], []))
            self.assertIn("r.roomid", condition)
        with patch.dict(viewer.os.environ, {"VIEWER_OFFICIAL_GROUP_KEYWORDS": "官群,客_服", "VIEWER_OFFICIAL_GROUP_IDS": "room1,room2"}):
            _, params = viewer.official_group_rule()
            self.assertEqual(params, (["%官群%", r"%客\_服%"], ["room1", "room2"]))

    def test_password_login_limit_returns_429_before_authentication(self):
        with patch.object(viewer, "rate_allowed", return_value=False), patch.object(viewer, "audit"):
            with viewer.app.test_request_context("/api/login", method="POST", json={"username": "admin", "password": "wrong"}):
                response, status = viewer.login()
        self.assertEqual(status, 429)
        self.assertIn("频繁", response.get_json()["error"])

    def test_backup_is_consistent_and_private(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / "data"
            data.mkdir()
            database = data / "viewer_users.db"
            with closing(sqlite3.connect(database)) as conn:
                conn.execute("CREATE TABLE users (name TEXT)")
                conn.execute("INSERT INTO users VALUES ('sample')")
                conn.commit()
            (data / ".env").write_text("VIEWER_SECRET_KEY=sample\n", encoding="utf-8")
            with patch.object(viewer, "DATA_DIR", data), patch.object(viewer, "USER_DB", database), patch("sys.argv", ["backup_data", "--output-dir", str(root / "backups")]):
                backup_data.main()
            target = next((root / "backups").iterdir())
            with closing(sqlite3.connect(target / "viewer_users.db")) as conn:
                self.assertEqual(conn.execute("SELECT name FROM users").fetchone()[0], "sample")
            self.assertIn(backup_data.digest(target / "viewer_users.db"), (target / "SHA256SUMS").read_text())
            self.assertEqual((target / ".env").read_text(), "VIEWER_SECRET_KEY=sample\n")


if __name__ == "__main__":
    unittest.main()
