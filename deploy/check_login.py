"""Check a local password login without printing or logging the password or hash."""
import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app  # noqa: E402
from werkzeug.security import check_password_hash  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="检查本地账号密码及登录 Cookie 配置")
    parser.add_argument("--username", default="admin")
    args = parser.parse_args()
    print("user_db:", app.USER_DB)
    with app.user_db() as conn:
        row = conn.execute("SELECT role,enabled,password_hash FROM users WHERE username=?", (args.username,)).fetchone()
    print("account_exists:", bool(row))
    print("account_enabled:", bool(row["enabled"]) if row else False)
    print("account_role:", row["role"] if row else "-")
    print("secure_cookie:", app.config["SESSION_COOKIE_SECURE"])
    print("https_origin_ready:", bool(app.sso_origin()))
    if not row:
        return
    password = getpass.getpass("登录密码（输入不显示）：")
    print("password_matches:", check_password_hash(row["password_hash"], password))


if __name__ == "__main__":
    main()
