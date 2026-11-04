"""Interactively create or reset an administrator without showing the password in shell history."""
import argparse
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="重置管理员密码")
    parser.add_argument("--username", default=os.getenv("VIEWER_ADMIN_USER", "admin"))
    args = parser.parse_args()
    if not args.username:
        raise SystemExit("管理员用户名不能为空，请用 --username 指定。")
    with app.user_db() as conn:
        row = conn.execute("SELECT id,role FROM users WHERE username=?", (args.username,)).fetchone()
    if row and row["role"] != "admin":
        raise SystemExit(f"{args.username} 已是普通用户，请用 --username 指定其他管理员用户名。")
    password = getpass.getpass("新密码（输入时不显示）：")
    if not password:
        raise SystemExit("密码不能为空")
    with app.user_db() as conn:
        if row:
            conn.execute("UPDATE users SET password_hash=?,enabled=1 WHERE id=?", (generate_password_hash(password), row["id"]))
        else:
            conn.execute("INSERT INTO users(username,password_hash,role,enabled,menu_permissions) VALUES(?,?,?,1,?)",
                         (args.username, generate_password_hash(password), "admin", app.DEFAULT_MENUS))
    print(f"管理员 {args.username} 已{'重设密码并启用' if row else '创建'}。")


if __name__ == "__main__":
    main()
