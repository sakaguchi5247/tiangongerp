# -*- coding: utf-8 -*-
"""认证、用户与操作日志"""
from .. import db, security
from ..utils import now
from ..config import ROLE_NAMES, dump_perms, parse_perms


def authenticate(username: str, password: str):
    """校验登录，成功返回用户字典，失败返回 None"""
    if not username or not password:
        return None
    with db.txn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username=? AND active=1", (username.strip(),)
        ).fetchone()
        if not row:
            return None
        if not security.verify_password(password, row["password_hash"], row["salt"]):
            return None
        conn.execute("UPDATE users SET last_login=? WHERE id=?", (now(), row["id"]))
        log(conn, row["id"], row["username"], "登录", "系统", "", "用户登录成功")
        u = dict(row)
        # v1.5.1：出厂默认口令/被重置过的账号登录后必须先改口令才能进业务功能
        u["must_change"] = int(row["must_change"]) if "must_change" in row.keys() else 0
        return u


def change_password(user_id: int, old_password: str, new_password: str) -> tuple:
    with db.txn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row or not security.verify_password(old_password, row["password_hash"], row["salt"]):
            return False, "原密码不正确"
        if not new_password or len(new_password) < 6:
            return False, "新密码至少需要 6 位"
        h, s = security.hash_password(new_password)
        conn.execute("UPDATE users SET password_hash=?, salt=?, must_change=0 WHERE id=?",
                     (h, s, user_id))
        log(conn, user_id, row["username"], "修改密码", "系统")
        return True, "密码已更新"


def list_users(keyword: str = ""):
    with db.txn() as conn:
        sql = ("SELECT id, username, full_name, role, perms, must_change, active, last_login,"
               " created_at FROM users")
        args = []
        if keyword:
            sql += " WHERE username LIKE ? OR full_name LIKE ?"
            args += [f"%{keyword}%", f"%{keyword}%"]
        sql += " ORDER BY id"
        rows = db.rows_to_dicts(conn.execute(sql, args).fetchall())
    for r in rows:
        r["perms_dict"] = parse_perms(r.get("perms"))     # None 表示沿用角色默认
    return rows


def get_user(user_id: int):
    with db.txn() as conn:
        row = conn.execute("SELECT id, username, full_name, role, perms, must_change, active,"
                           " last_login, created_at FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def create_user(username, full_name, role, password, active=1, perms=None):
    if role not in ROLE_NAMES:
        return False, "角色不存在"
    username = (username or "").strip()
    if not username:
        return False, "用户名不能为空"
    perms_str = dump_perms(perms) if perms else ""
    if username == "admin" and perms_str:
        return False, "内置管理员拥有全部权限，无需单独分配"
    with db.txn() as conn:
        exists = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if exists:
            return False, "用户名已存在"
        h, s = security.hash_password(password or "123456")
        # 没给口令（用默认口令创建）的，首次登录强制改密
        conn.execute(
            "INSERT INTO users(username, full_name, password_hash, salt, role, perms, must_change, active)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (username, full_name or username, h, s, role, perms_str,
             0 if password else 1, int(active)),
        )
    return True, "用户已创建"


def update_user(user_id, full_name, role, active, password=None, perms=None):
    if role not in ROLE_NAMES:
        return False, "角色不存在"
    with db.txn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            return False, "用户不存在"
        if row["username"] == "admin" and int(active) == 0:
            return False, "内置管理员不可停用"
        if row["username"] == "admin":
            role = "admin"
            perms = None                                  # 管理员权限不可被削弱
        perms_str = dump_perms(perms) if perms is not None else (row["perms"] or "")
        conn.execute("UPDATE users SET full_name=?, role=?, perms=?, active=? WHERE id=?",
                     (full_name, role, perms_str, int(active), user_id))
        if password:
            h, s = security.hash_password(password)
            # 管理员代改的口令同样要求本人首次登录后修改
            conn.execute("UPDATE users SET password_hash=?, salt=?, must_change=1 WHERE id=?",
                         (h, s, user_id))
    return True, "用户已更新"


def set_perms(user_id, perms):
    """单独设置某账号的分区权限（dict 或 JSON 字符串；空值=恢复角色默认）"""
    with db.txn() as conn:
        row = conn.execute("SELECT username, role FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            return False, "用户不存在"
        if row["username"] == "admin":
            return False, "内置管理员拥有全部权限，不可修改"
        conn.execute("UPDATE users SET perms=? WHERE id=?", (dump_perms(perms), user_id))
    return True, "分区权限已保存"


def delete_user(user_id):
    with db.txn() as conn:
        row = conn.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
        if row and row["username"] == "admin":
            return False, "内置管理员不可删除"
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    return True, "用户已删除"


# ------------------------------------------------------------------ 日志
def log(conn, user_id, username, action, module, ref_no="", detail=""):
    conn.execute(
        "INSERT INTO audit_logs(user_id, username, action, module, ref_no, detail, created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (user_id, username, action, module, ref_no, detail, now()),
    )


def write_log(user: dict, action: str, module: str, ref_no: str = "", detail: str = ""):
    with db.txn() as conn:
        log(conn, (user or {}).get("id"), (user or {}).get("username", ""),
            action, module, ref_no, detail)


def list_logs(keyword: str = "", limit: int = 300):
    with db.txn() as conn:
        sql = ("SELECT * FROM audit_logs")
        args = []
        if keyword:
            sql += " WHERE username LIKE ? OR action LIKE ? OR module LIKE ? OR ref_no LIKE ?"
            args += [f"%{keyword}%"] * 4
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return db.rows_to_dicts(conn.execute(sql, args).fetchall())
