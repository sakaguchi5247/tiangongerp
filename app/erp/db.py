# -*- coding: utf-8 -*-
"""数据库连接与初始化"""
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

from .config import DB_PATH, DEFAULT_ADMIN, DEFAULT_PARAMS
from . import security

# 打包成 exe（PyInstaller frozen）时，源码被解压到 sys._MEIPASS，
# schema.sql 随之落在 <_MEIPASS>/erp/schema.sql；源码运行则取本文件同目录。
_MEIPASS = Path(getattr(sys, "_MEIPASS", "")) if getattr(sys, "frozen", False) else None
SCHEMA_PATH = (_MEIPASS / "erp" / "schema.sql") if _MEIPASS else (Path(__file__).parent / "schema.sql")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    # busy_timeout：被其它连接短暂持锁时自动重试（最多 30s），避免「database is locked」。
    # 市场对接属于并发 API 写入场景，短事务 + 该重试是消除锁竞争的关键。
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


class OpAbort(Exception):
    """业务操作中止。

    服务层在事务中途发现业务规则不满足时抛出（携带给用户看的失败原因），
    txn() 会捕获并回滚，调用方拿到 (False, 原因)。
    用途：杜绝「循环里前半段已改库、后半段失败却仍被提交」这类半截写入。
    """


@contextmanager
def txn():
    """业务事务上下文：成功→提交；OpAbort 或任何异常→回滚；无论如何→关闭连接。

    替代过去的 `with db.connect() as conn`：
    旧写法只做提交/回滚而从不 close，长时间运行的 Web 服务会持续泄漏
    SQLite 连接与文件句柄，未关闭的连接还会持有写锁导致 "database is locked"。
    """
    conn = connect()
    try:
        yield conn
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def init_db(verbose: bool = False):
    """建表 + 轻量迁移 + 初始化管理员与基础参数"""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with txn() as conn:
        conn.executescript(sql)
        _migrate(conn)
        _ensure_admin(conn)
        _ensure_params(conn)
        _ensure_basics(conn)
        _ensure_zones(conn)
    if verbose:
        print(f"[ERP] 数据库已就绪：{DB_PATH}")
    return DB_PATH


# 已有库缺列时补列。CREATE TABLE IF NOT EXISTS 不会自动补列，
# 新增字段必须在此登记，否则老库升级后读该列会报错。
_COLUMN_MIGRATIONS = {
    "users": {
        "perms": "TEXT NOT NULL DEFAULT ''",
        "must_change": "INTEGER NOT NULL DEFAULT 0",
    },
    "partners": {"remark": "TEXT DEFAULT ''"},
    "sale_items": {"delivered_at": "TEXT DEFAULT ''"},
    "purchase_items": {"received_at": "TEXT DEFAULT ''"},
    "stock_moves": {"zone_id": "INTEGER REFERENCES zones(id)"},
    "sale_orders": {
        "channel": "TEXT NOT NULL DEFAULT ''",
        "platform_order_no": "TEXT NOT NULL DEFAULT ''",
        "currency": "TEXT NOT NULL DEFAULT 'CNY'",
        "rate": "REAL NOT NULL DEFAULT 1",
        "foreign_amount": "REAL NOT NULL DEFAULT 0",
    },
    "purchase_orders": {
        "currency": "TEXT NOT NULL DEFAULT 'CNY'",
        "rate": "REAL NOT NULL DEFAULT 1",
        "foreign_amount": "REAL NOT NULL DEFAULT 0",
    },
    "payments": {
        "currency": "TEXT NOT NULL DEFAULT 'CNY'",
        "rate": "REAL NOT NULL DEFAULT 1",
        "foreign_amount": "REAL NOT NULL DEFAULT 0",
    },
}


def _migrate(conn: sqlite3.Connection):
    for table, cols in _COLUMN_MIGRATIONS.items():
        try:
            exist = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            continue
        if not exist:                      # 表还不存在，交给 schema 建
            continue
        for col, ddl in cols.items():
            if col not in exist:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    _ensure_return_tables(conn)
    _ensure_marketplace_tables(conn)


def _ensure_return_tables(conn: sqlite3.Connection):
    """v2.2.0：旧库升级时补齐退货单两张表（CREATE TABLE IF NOT EXISTS 幂等）。

    新库由 schema.sql 在建库阶段直接建好；此处只为「已部署的 v2.1.0 库升级」
    兜底，保证退货模块在老库上也能直接用。
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS return_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rt_no TEXT NOT NULL UNIQUE,
            rt_type TEXT NOT NULL DEFAULT 'SALE_RETURN',
            ref_type TEXT NOT NULL DEFAULT '',
            ref_id INTEGER,
            partner_id INTEGER NOT NULL REFERENCES partners(id),
            warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
            order_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'DRAFT',
            total_amount REAL NOT NULL DEFAULT 0,
            remark TEXT DEFAULT '',
            created_by TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS return_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rto_id INTEGER NOT NULL REFERENCES return_orders(id) ON DELETE CASCADE,
            product_id INTEGER NOT NULL REFERENCES products(id),
            ref_line_id INTEGER,
            qty REAL NOT NULL DEFAULT 0,
            unit_price REAL NOT NULL DEFAULT 0,
            amount REAL NOT NULL DEFAULT 0,
            sort_no INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rto_items ON return_items(rto_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rto_ref ON return_orders(ref_type, ref_id)")


def _ensure_marketplace_tables(conn: sqlite3.Connection):
    """v3.0：旧库升级时补齐市场对接三张表与销售单幂等索引（CREATE TABLE IF NOT EXISTS 幂等）。

    新库由 schema.sql 在建库阶段直接建好；此处只为「已部署的 v2.2.0 及更早库升级」兜底，
    保证市场对接模块在老库上也能直接用，且幂等可重复执行。
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sku_map (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL REFERENCES products(id),
            channel TEXT NOT NULL,
            platform_sku TEXT NOT NULL DEFAULT '',
            logistics_code TEXT DEFAULT '',
            extra TEXT DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            UNIQUE(product_id, channel)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sku_map_channel ON sku_map(channel, platform_sku)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS channel_cred (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT NOT NULL UNIQUE,
            shop TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'INACTIVE',
            access_token TEXT DEFAULT '',
            refresh_token TEXT DEFAULT '',
            token_secret TEXT DEFAULT '',
            expires_at TEXT DEFAULT '',
            cfg TEXT DEFAULT '',
            note TEXT DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT NOT NULL,
            biz_type TEXT NOT NULL,
            platform_no TEXT DEFAULT '',
            local_no TEXT DEFAULT '',
            direction TEXT NOT NULL DEFAULT 'PULL',
            status TEXT NOT NULL DEFAULT 'OK',
            detail TEXT DEFAULT '',
            synced_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_channel ON sync_log(channel, biz_type, platform_no)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_so_channel_platform "
        "ON sale_orders(channel, platform_order_no) WHERE channel <> ''"
    )


def _ensure_admin(conn: sqlite3.Connection):
    row = conn.execute("SELECT COUNT(*) c FROM users").fetchone()
    if row["c"] == 0:
        h, s = security.hash_password(DEFAULT_ADMIN[1])
        # must_change=1：出厂口令登录后强制改成自己的口令，避免默认口令长期沿用
        conn.execute(
            "INSERT INTO users(username, full_name, password_hash, salt, role, must_change, active)"
            " VALUES(?,?,?,?,'admin',1,1)",
            (DEFAULT_ADMIN[0], "系统管理员", h, s),
        )


def _ensure_params(conn: sqlite3.Connection):
    for k, v in DEFAULT_PARAMS.items():
        conn.execute(
            "INSERT OR IGNORE INTO sys_params(key, value, note) VALUES(?,?,?)",
            (k, str(v), "系统默认经营参数"),
        )


def _ensure_basics(conn: sqlite3.Connection):
    """默认仓库与分类，保证库存模块可直接使用"""
    if conn.execute("SELECT COUNT(*) c FROM warehouses").fetchone()["c"] == 0:
        conn.executemany(
            "INSERT OR IGNORE INTO warehouses(code, name, address) VALUES(?,?,?)",
            [("WH01", "主仓库", "上海市松江区工业区1号"),
             ("WH02", "备用仓", "上海市松江区工业区2号")],
        )
    if conn.execute("SELECT COUNT(*) c FROM categories").fetchone()["c"] == 0:
        conn.executemany(
            "INSERT OR IGNORE INTO categories(code, name) VALUES(?,?)",
            [("C01", "原材料"), ("C02", "成品"), ("C03", "包装耗材"), ("C04", "办公用品")],
        )


# 默认库区模板：每个仓库自动生成这 5 个区，库存默认落在「存货区」。
_DEFAULT_ZONES = [
    ("Z01", "存货区", "STORAGE", 0, "#2563EB", 10),
    ("Z02", "退货区", "RETURN", 0, "#DC2626", 20),
    ("Z03", "待检区", "INSPECT", 0, "#D97706", 30),
    ("Z04", "残次区", "DEFECT", 0, "#7C3AED", 40),
    ("Z05", "暂存区", "STAGING", 0, "#0891B2", 50),
]


def _ensure_zones(conn: sqlite3.Connection):
    """为每个仓库补齐默认库区，并把已有库存回填进「存货区」，保证升级后分区对账成立。

    - 新建库：自动带 5 个区。
    - 老库升级：库区缺失才补；已有库存（warehouse 级）按「存货区」回填到 zone_stock，
      使 Σzone_stock == inventory 立即成立，可视化数字与进销存台账严格一致。
    - 自愈：清理孤儿 zone_stock 行（历史版本清空业务数据时漏清分区库存，
      会导致「库存管理为空、仓库可视化有数、对账差异非零」的三重矛盾，v1.6.0 修复）。
    """
    # 自愈先行：商品/仓库/库区档案已删、分区库存却残留的行，先清掉再回填
    conn.execute(
        "DELETE FROM zone_stock WHERE product_id NOT IN (SELECT id FROM products)"
        " OR warehouse_id NOT IN (SELECT id FROM warehouses)"
        " OR zone_id NOT IN (SELECT id FROM zones)")
    for wh in conn.execute("SELECT id FROM warehouses WHERE active=1"):
        wid = wh["id"]
        has = conn.execute(
            "SELECT COUNT(*) c FROM zones WHERE warehouse_id=?", (wid,)).fetchone()["c"]
        if has == 0:
            conn.executemany(
                "INSERT INTO zones(warehouse_id, code, name, zone_type, capacity, color, sort_order)"
                " VALUES(?,?,?,?,?,?,?)",
                [(wid, c, n, t, cap, col, so) for (c, n, t, cap, col, so) in _DEFAULT_ZONES])
        # 回填：把存货区缺的 zone_stock 行补齐（仅补缺失，不动已存在分区）
        storage = conn.execute(
            "SELECT id FROM zones WHERE warehouse_id=? AND zone_type='STORAGE' ORDER BY id LIMIT 1",
            (wid,)).fetchone()
        if not storage:
            continue
        sid = storage["id"]
        rows = conn.execute(
            "SELECT i.product_id, i.qty FROM inventory i "
            "WHERE i.warehouse_id=? AND i.qty<>0 "
            "AND NOT EXISTS(SELECT 1 FROM zone_stock z WHERE z.product_id=i.product_id "
            "  AND z.warehouse_id=i.warehouse_id AND z.zone_id=?)",
            (wid, sid)).fetchall()
        for r in rows:
            conn.execute(
                "INSERT OR IGNORE INTO zone_stock(product_id, warehouse_id, zone_id, qty) VALUES(?,?,?,?)",
                (r["product_id"], wid, sid, r["qty"]))


def get_params(conn: sqlite3.Connection = None) -> dict:
    own = conn is None
    conn = conn or connect()
    try:
        rows = conn.execute("SELECT key, value FROM sys_params").fetchall()
        out = dict(DEFAULT_PARAMS)
        for r in rows:
            try:
                out[r["key"]] = float(r["value"])
            except ValueError:
                out[r["key"]] = r["value"]
        return out
    finally:
        if own:
            conn.close()


def set_param(key: str, value):
    with txn() as conn:
        conn.execute("UPDATE sys_params SET value=? WHERE key=?", (str(value), key))


def next_no(conn: sqlite3.Connection, name: str, prefix: str) -> str:
    """生成单据流水号：前缀 + 年月日 + 4 位序号"""
    import datetime
    today = datetime.date.today().strftime("%Y%m%d")
    conn.execute("INSERT OR IGNORE INTO sequences(name, val, prefix) VALUES(?,0,?)", (name, prefix))
    row = conn.execute("SELECT val, updated_on FROM sequences WHERE name=?", (name,)).fetchone()
    val = row["val"]
    if row["updated_on"] != today:
        val = 0
    val += 1
    conn.execute("UPDATE sequences SET val=?, updated_on=? WHERE name=?", (val, today, name))
    return f"{prefix}{today}{val:04d}"


def rows_to_dicts(rows) -> list:
    return [dict(r) for r in rows]


def clean_business_data(remove_demo_users: bool = True, keep_username: str = "admin"):
    """清空全部业务数据（演示数据），只保留基础配置与管理员账号。

    清空：商品、往来单位、库存余额、库区库存分布(zone_stock)、库存流水、采购/销售订单及明细、收付款、操作日志、单号序列
    保留：仓库、分类、库区档案(zones)、经营参数(sys_params)、指定的管理员账号

    注意：zone_stock 必须与 inventory 同步清理——只清 inventory 会让
    「库存管理为空但仓库可视化有数」的两套账不一致（v1.6.0 修复）。
    """
    tables = ["payments", "stock_moves", "inventory", "zone_stock",
              "return_items", "return_orders",
              "sku_map", "channel_cred", "sync_log",
              "purchase_items", "purchase_orders",
              "sale_items", "sale_orders",
              "products", "partners", "audit_logs", "sequences"]
    with txn() as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            for t in tables:
                conn.execute(f"DELETE FROM {t}")
            if remove_demo_users:
                conn.execute("DELETE FROM users WHERE username <> ?", (keep_username,))
            try:                                  # 让主键与单号从 1 重新开始
                conn.execute("DELETE FROM sqlite_sequence")
            except sqlite3.Error:
                pass
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
    return True


def reset_db():
    """重置数据库：删除库文件并重建（用于恢复演示数据）。

    若库文件被占用/无权限删除，则退化为「清空全部业务数据」，效果等价，不会中断。
    """
    removed = True
    try:
        DB_PATH.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            p = Path(str(DB_PATH) + suffix)
            p.unlink(missing_ok=True)
    except OSError:
        removed = False
    if not removed and DB_PATH.exists():
        try:
            clean_business_data(remove_demo_users=True)
        except sqlite3.Error:
            pass
    return init_db(verbose=True)
