"""Create a consistent, private backup of the viewer's persistent local data."""
import argparse
import hashlib
import os
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def main():
    parser = argparse.ArgumentParser(description="备份用户库和配置；不会覆盖已有备份")
    parser.add_argument("--output-dir", default="/opt/archive-backups")
    args = parser.parse_args()
    destination = Path(args.output_dir).expanduser().resolve()
    data_dir = app.DATA_DIR.resolve()
    if destination == data_dir or destination.is_relative_to(data_dir):
        parser.error("备份目录必须位于 /opt/archive-data 之外")
    config = data_dir / ".env"
    if not config.is_file() or not app.USER_DB.is_file():
        parser.error("配置文件或用户库不存在")
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(destination, 0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = destination / stamp
    target.mkdir(mode=0o700)
    try:
        database = target / "viewer_users.db"
        with closing(sqlite3.connect(app.USER_DB)) as source, closing(sqlite3.connect(database)) as backup:
            source.backup(backup)
        with closing(sqlite3.connect(database)) as check:
            if check.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("备份用户库校验失败")
        settings = target / ".env"
        shutil.copyfile(config, settings)
        os.chmod(database, 0o600)
        os.chmod(settings, 0o600)
        manifest = target / "SHA256SUMS"
        manifest.write_text(f"{digest(database)}  viewer_users.db\n{digest(settings)}  .env\n", encoding="ascii")
        os.chmod(manifest, 0o600)
    except Exception:
        shutil.rmtree(target)
        raise
    print(f"备份完成：{target}（用户库一致性与文件摘要已检查）")


if __name__ == "__main__":
    main()
