"""Read-only SSO diagnostics. Never prints a key, digest, or ticket."""
import argparse
import getpass
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app  # noqa: E402


def show_account(row):
    print(f"account id={row['id']} username={row['username']} role={row['role']} "
          f"enabled={bool(row['enabled'])} authorized={bool(row['sso_key_hash'] and row['sso_key_version'])} "
          f"external_user_id={row['sso_external_user_id'] or '-'} created_at={row['sso_created_at'] or '-'}")


def main():
    parser = argparse.ArgumentParser(description="检查免登授权与最近审计记录")
    parser.add_argument("--user", action="append", default=[], help="查询本地账号用户名，可重复")
    parser.add_argument("--check-key", action="store_true", help="在终端隐式输入一把长期密钥并检查绑定账号")
    args = parser.parse_args()
    print("public_origin_ready:", bool(app.sso_origin()))
    print("auth_db:", app.USER_DB)
    with app.user_db() as conn:
        for name in args.user:
            row = conn.execute("SELECT id,username,role,enabled,sso_key_hash,sso_key_version,sso_created_at,sso_external_user_id "
                               "FROM users WHERE username=? OR sso_external_user_id=?", (name, name)).fetchone()
            if row:
                show_account(row)
            else:
                print(f"local username {name}: not found")
        if args.check_key:
            key = getpass.getpass("免登密钥（输入不显示）：")
            digest = hashlib.sha256(key.encode("utf-8")).hexdigest() if key else ""
            row = conn.execute("SELECT id,username,role,enabled,sso_key_hash,sso_key_version,sso_created_at,sso_external_user_id "
                               "FROM users WHERE sso_key_hash=?", (digest,)).fetchone()
            if row:
                print("key_match: yes")
                show_account(row)
            else:
                print("key_match: no (可能使用了旧密钥、已重置/吊销，或连接了另一份用户库)")
        rows = conn.execute("SELECT created_at,action,detail FROM audit_events "
                            "WHERE action LIKE '%免登%' ORDER BY id DESC LIMIT 15").fetchall()
    print("recent_sso_audit:")
    for row in rows:
        print(f"  {row['created_at']} {row['action']} {row['detail']}")


if __name__ == "__main__":
    main()
