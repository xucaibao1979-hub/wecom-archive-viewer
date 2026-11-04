"""Safely copy configuration and SQLite data outside the release tree."""
import argparse
import shutil
import sqlite3
from pathlib import Path


def migrate():
    parser = argparse.ArgumentParser(description="迁移配置和用户库至 /opt/archive-data")
    parser.add_argument("--db-source", type=Path, help="有多个旧用户库时明确指定源文件")
    args = parser.parse_args()
    release = Path(__file__).resolve().parents[1]
    data = Path("/opt/archive-data")
    if not data.is_dir():
        raise SystemExit(f"请先创建目录：{data}")

    env_target = data / ".env"
    env_source = release / ".env"
    if env_source.is_file() and not env_target.exists():
        shutil.copy2(env_source, env_target)
        env_target.chmod(0o600)
        print(f"已复制配置：{env_source} -> {env_target}")
    elif env_target.exists():
        print(f"配置已存在，未覆盖：{env_target}")
    else:
        print(f"未找到旧配置；请从备份恢复或新建 {env_target}")

    target = data / "viewer_users.db"
    if target.exists():
        print(f"用户库已存在，未覆盖：{target}")
        return
    candidates = [release / "viewer_users.db", Path("/var/lib/archive-viewer/viewer_users.db")]
    sources = [p for p in candidates if p.is_file()]
    if args.db_source:
        source = args.db_source.resolve()
        if not source.is_file():
            raise SystemExit(f"指定的源用户库不存在：{source}")
    elif len(sources) == 1:
        source = sources[0]
    elif len(sources) > 1:
        raise SystemExit("找到多个旧用户库；请用 --db-source 指定正确来源：" + ", ".join(map(str, sources)))
    else:
        raise SystemExit("未找到旧用户库；请先从备份恢复，不要启动空库。")
    try:
        with sqlite3.connect(source) as old, sqlite3.connect(target) as new:
            if old.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("旧用户库完整性检查失败")
            old.backup(new)
            count = new.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            if new.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("目标用户库完整性检查失败")
    except Exception:
        target.unlink(missing_ok=True)
        raise
    target.chmod(0o600)
    print(f"已迁移 {count} 个用户：{source} -> {target}；原文件保留。")


if __name__ == "__main__":
    migrate()
