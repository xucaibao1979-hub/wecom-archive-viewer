"""Verify that a scheduled archive can restore the persistent local data."""

import sqlite3
import tarfile
import tempfile
import unittest
from contextlib import redirect_stderr
from contextlib import closing
from datetime import date
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from deploy import backup_project


class ProjectBackupTest(unittest.TestCase):
    def test_bundle_contains_code_and_consistent_data_without_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, data, output = (root / name for name in ("project", "data", "backups"))
            project.mkdir()
            data.mkdir()
            output.mkdir()
            (project / "app.py").write_text("print('ok')\n", encoding="utf-8")
            (project / ".env.example").write_text("EXAMPLE=1\n", encoding="utf-8")
            (project / ".env").write_text("secret\n", encoding="utf-8")
            (project / ".venv").mkdir()
            (project / ".venv" / "secret").write_text("excluded", encoding="utf-8")
            (project / ".cache").mkdir()
            (project / ".cache" / "download").write_text("excluded", encoding="utf-8")
            (data / ".env").write_text("VIEWER_SECRET_KEY=example\n", encoding="utf-8")
            (data / "other.txt").write_text("persisted\n", encoding="utf-8")
            (data / "cache").mkdir()
            (data / "cache" / "audio.wav").write_bytes(b"excluded")
            with closing(sqlite3.connect(data / "viewer_users.db")) as db:
                db.execute("CREATE TABLE users (name TEXT)")
                db.execute("INSERT INTO users VALUES ('sample')")
                db.commit()
            old = output / "archive-viewer-20200101.tar.gz"
            old.write_bytes(b"old")
            arguments = ["backup_project", "--project-dir", str(project), "--data-dir", str(data), "--output-dir", str(output)]
            with patch("sys.argv", arguments):
                backup_project.main()
            bundle = output / f"archive-viewer-{date.today():%Y%m%d}.tar.gz"
            with tarfile.open(bundle) as archive:
                names = set(archive.getnames())
                self.assertTrue({"archive-viewer/app.py", "archive-viewer/.env.example", "archive-data/.env", "archive-data/viewer_users.db", "archive-data/other.txt"} <= names)
                self.assertFalse({"archive-viewer/.env", "archive-viewer/.venv/secret", "archive-viewer/.cache/download", "archive-data/cache/audio.wav"} & names)
                with tempfile.TemporaryDirectory() as extracted:
                    archive.extract("archive-data/viewer_users.db", path=extracted, filter="data")
                    with closing(sqlite3.connect(Path(extracted) / "archive-data" / "viewer_users.db")) as db:
                        self.assertEqual(db.execute("SELECT name FROM users").fetchone()[0], "sample")
            self.assertFalse(old.exists())
            with patch("sys.argv", arguments), redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                backup_project.main()


if __name__ == "__main__":
    unittest.main()
