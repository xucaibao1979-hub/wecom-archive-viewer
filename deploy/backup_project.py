"""Back up deployed viewer code and persistent local data for cron on Ubuntu."""

import argparse
import hashlib
import os
import shutil
import sqlite3
import tarfile
import tempfile
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path


SKIP_CODE_DIRS = {".git", ".venv", ".cache", "__pycache__", ".pytest_cache", "cache", "dist"}
SKIP_CODE_SUFFIXES = (".db", ".db-shm", ".db-wal", ".log", ".zip", ".pyc")


def permitted_code(path):
    if path.name in SKIP_CODE_DIRS or (path.name.startswith(".env") and path.name != ".env.example"):
        return False
    if path.is_file() and path.name.endswith(SKIP_CODE_SUFFIXES):
        return False
    return True


def copy_data(source, target, user_db):
    target.mkdir(mode=0o700)
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if "cache" in relative.parts or path.is_symlink():
            continue
        if path == user_db or path.name in {user_db.name + "-wal", user_db.name + "-shm"}:
            continue
        destination = target / relative
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)

    database = target / user_db.relative_to(source)
    database.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(user_db)) as original, closing(sqlite3.connect(database)) as backup:
        original.backup(backup)
    with closing(sqlite3.connect(database)) as check:
        if check.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite 用户库备份校验失败")


def main():
    parser = argparse.ArgumentParser(description="按日备份项目代码和本地持久化数据，保留 30 天")
    parser.add_argument("--project-dir", type=Path, default=Path("/opt/archive-viewer"))
    parser.add_argument("--data-dir", type=Path, default=Path("/opt/archive-data"))
    parser.add_argument("--output-dir", type=Path, default=Path("/opt/archive-backups"))
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days 必须大于 0")
    project = args.project_dir.resolve(strict=True)
    data = args.data_dir.resolve(strict=True)
    output = args.output_dir.resolve()
    if project == data or project.is_relative_to(data) or data.is_relative_to(project):
        parser.error("代码目录和数据目录不能互相包含")
    if output == project or output.is_relative_to(project) or output == data or output.is_relative_to(data):
        parser.error("备份目录不能位于代码或数据目录内")
    user_db = (data / "viewer_users.db").resolve(strict=True)
    if not user_db.is_relative_to(data) or not (data / ".env").is_file():
        parser.error("找不到 /opt/archive-data/.env 或有效的 viewer_users.db")

    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(output, 0o700)
    today = date.today()
    destination = output / f"archive-viewer-{today:%Y%m%d}.tar.gz"
    if destination.exists():
        parser.error(f"今天的备份已存在：{destination}")

    with tempfile.TemporaryDirectory(prefix=".archive-viewer-", dir=output) as temporary:
        stage = Path(temporary)
        copy_data(data, stage / "data", user_db)
        archive = stage / "backup.tar.gz"
        with tarfile.open(archive, "w:gz", dereference=False) as bundle:
            bundle.add(project, arcname="archive-viewer", filter=lambda info: info if permitted_code(Path(info.name)) else None)
            bundle.add(stage / "data", arcname="archive-data")
        with tarfile.open(archive, "r:gz") as check:
            names = set(check.getnames())
            if "archive-data/.env" not in names or "archive-data/viewer_users.db" not in names:
                raise RuntimeError("备份包缺少配置或用户库")
        os.chmod(archive, 0o600)
        os.replace(archive, destination)

    checksum = hashlib.sha256()
    with destination.open("rb") as saved:
        for chunk in iter(lambda: saved.read(1024 * 1024), b""):
            checksum.update(chunk)
    cutoff = today - timedelta(days=args.days - 1)
    removed = 0
    for old in output.glob("archive-viewer-????????.tar.gz"):
        try:
            backup_day = date.fromisoformat(old.name[15:23])
        except ValueError:
            continue
        if backup_day < cutoff:
            old.unlink()
            removed += 1
    print(f"备份完成：{destination}")
    print(f"SHA-256：{checksum.hexdigest()}；清理旧备份：{removed} 个")
    print("注意：外部 PostgreSQL 会话库和 VIEWER_MEDIA_ROOT 媒体目录需另行备份。")


if __name__ == "__main__":
    main()
