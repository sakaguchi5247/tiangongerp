# -*- coding: utf-8 -*-
"""主数据服务：分类 / 仓库 / 商品 / 往来单位"""
import sqlite3

from .. import db
from ..utils import now, safe_float, safe_int


def _guard(conn, checks) -> str:
    """逐条检查档案是否被业务数据引用，返回第一条命中的提示文案；无引用返回空串。"""
    for sql, args, tmpl in checks:
        n = conn.execute(sql, args).fetchone()[0]
        if n:
            return tmpl.format(n=n)
    return ""


def _delete_guard(fn):
    """兜底：任何漏检的外键冲突都转成一句中文提示，不再抛 IntegrityError。"""
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except db.OpAbort as e:
            return False, str(e)
        except sqlite3.IntegrityError:
            return False, "该档案已被业务数据引用（数据库外键约束），建议改为「停用」而非删除"
    wrapper.__name__ = getattr(fn, "__name__", "wrapper")
    wrapper.__doc__ = getattr(fn, "__doc__", "")
    return wrapper


# ============================================================ 分类
def list_categories(keyword: str = ""):
    with db.txn() as conn:
        sql = "SELECT * FROM categories"
        args = []
        if keyword:
            sql += " WHERE code LIKE ? OR name LIKE ?"
            args += [f"%{keyword}%", f"%{keyword}%"]
        sql += " ORDER BY code"
        return db.rows_to_dicts(conn.execute(sql, args).fetchall())


def save_category(data: dict):
    with db.txn() as conn:
        if data.get("id"):
            conn.execute("UPDATE categories SET code=?, name=? WHERE id=?",
                         (data["code"], data["name"], data["id"]))
        else:
            conn.execute("INSERT INTO categories(code, name) VALUES(?,?)",
                         (data["code"], data["name"]))
    return True, "已保存"


def delete_category(cid: int):
    with db.txn() as conn:
        used = conn.execute("SELECT COUNT(*) c FROM products WHERE category_id=?", (cid,)).fetchone()["c"]
        if used:
            return False, f"该分类下还有 {used} 个商品，不能删除"
        conn.execute("DELETE FROM categories WHERE id=?", (cid,))
    return True, "已删除"


# ============================================================ 仓库
def list_warehouses(keyword: str = ""):
    with db.txn() as conn:
        sql = "SELECT * FROM warehouses"
        args = []
        if keyword:
            sql += " WHERE code LIKE ? OR name LIKE ?"
            args += [f"%{keyword}%", f"%{keyword}%"]
        sql += " ORDER BY code"
        return db.rows_to_dicts(conn.execute(sql, args).fetchall())


def save_warehouse(data: dict):
    with db.txn() as conn:
        if data.get("id"):
            conn.execute("UPDATE warehouses SET code=?, name=?, address=?, active=? WHERE id=?",
                         (data["code"], data["name"], data.get("address", ""),
                          data.get("active", 1), data["id"]))
        else:
            conn.execute("INSERT INTO warehouses(code, name, address, active) VALUES(?,?,?,?)",
                         (data["code"], data["name"], data.get("address", ""), data.get("active", 1)))
    return True, "已保存"


@_delete_guard
def delete_warehouse(wid: int):
    """v1.5.1（P1-2）：补全引用检查。

    过去只查 inventory 余额，漏掉 stock_moves / 采购 / 销售单，
    外键拦截后又直接把 IntegrityError 抛给界面（Web 500 / 桌面弹异常）。
    """
    with db.txn() as conn:
        msg = _guard(conn, [
            ("SELECT COUNT(*) FROM inventory WHERE warehouse_id=? AND qty<>0", (wid,),
             "该仓库仍有 {n} 个物料的库存余额，不能删除"),
            ("SELECT COUNT(*) FROM stock_moves WHERE warehouse_id=?", (wid,),
             "该仓库有 {n} 条出入库流水，删除会断裂库存追溯链，建议改为停用"),
            ("SELECT COUNT(*) FROM purchase_orders WHERE warehouse_id=?", (wid,),
             "该仓库关联 {n} 张采购订单，建议改为停用"),
            ("SELECT COUNT(*) FROM sale_orders WHERE warehouse_id=?", (wid,),
             "该仓库关联 {n} 张销售订单，建议改为停用"),
        ])
        if msg:
            raise db.OpAbort(msg)
        conn.execute("DELETE FROM inventory WHERE warehouse_id=?", (wid,))
        conn.execute("DELETE FROM warehouses WHERE id=?", (wid,))
    return True, "已删除"


# ============================================================ 商品
PRODUCT_SQL = """
SELECT p.*, c.name AS category_name,
       COALESCE((SELECT SUM(i.qty) FROM inventory i WHERE i.product_id=p.id),0) AS stock_qty,
       COALESCE((SELECT SUM(i.qty*i.avg_cost) FROM inventory i WHERE i.product_id=p.id),0) AS stock_value,
       (SELECT z.name FROM zone_stock zs JOIN zones z ON z.id=zs.zone_id
         WHERE zs.product_id=p.id AND zs.qty<>0
         ORDER BY (z.zone_type='STORAGE') DESC, zs.qty DESC LIMIT 1) AS primary_zone_name,
       (SELECT z.code FROM zone_stock zs JOIN zones z ON z.id=zs.zone_id
         WHERE zs.product_id=p.id AND zs.qty<>0
         ORDER BY (z.zone_type='STORAGE') DESC, zs.qty DESC LIMIT 1) AS primary_zone_code,
       (SELECT zs.qty FROM zone_stock zs JOIN zones z ON z.id=zs.zone_id
         WHERE zs.product_id=p.id AND zs.qty<>0
         ORDER BY (z.zone_type='STORAGE') DESC, zs.qty DESC LIMIT 1) AS primary_zone_qty,
       (SELECT zs.warehouse_id FROM zone_stock zs
         WHERE zs.product_id=p.id AND zs.qty<>0
         ORDER BY (SELECT z.zone_type FROM zones z WHERE z.id=zs.zone_id)='STORAGE' DESC, zs.qty DESC
         LIMIT 1) AS primary_zone_wh
FROM products p LEFT JOIN categories c ON c.id=p.category_id
"""


def list_products(keyword: str = "", category_id=None, only_active=False, low_stock=False):
    where, args = [], []
    if keyword:
        where.append("(p.sku LIKE ? OR p.name LIKE ? OR p.spec LIKE ? OR p.barcode LIKE ?)")
        args += [f"%{keyword}%"] * 4
    if category_id:
        where.append("p.category_id=?")
        args.append(category_id)
    if only_active:
        where.append("p.active=1")
    if low_stock:
        where.append("COALESCE((SELECT SUM(i.qty) FROM inventory i WHERE i.product_id=p.id),0) <= p.safety_stock")
    sql = PRODUCT_SQL
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY p.sku"
    with db.txn() as conn:
        return db.rows_to_dicts(conn.execute(sql, args).fetchall())


def get_product(pid: int):
    with db.txn() as conn:
        row = conn.execute(PRODUCT_SQL + " WHERE p.id=?", (pid,)).fetchone()
        return dict(row) if row else None


def get_product_by_sku(sku: str):
    if not sku:
        return None
    with db.txn() as conn:
        row = conn.execute(PRODUCT_SQL + " WHERE p.sku=?", (sku.strip(),)).fetchone()
        return dict(row) if row else None


def save_product(data: dict):
    fields = dict(
        sku=data.get("sku", "").strip(),
        name=data.get("name", "").strip(),
        category_id=data.get("category_id") or None,
        spec=data.get("spec", ""),
        unit=data.get("unit", "件"),
        barcode=data.get("barcode", ""),
        cost_price=safe_float(data.get("cost_price")),
        sale_price=safe_float(data.get("sale_price")),
        safety_stock=safe_float(data.get("safety_stock")),
        lead_time_days=safe_int(data.get("lead_time_days"), 7),
        reorder_point=safe_float(data.get("reorder_point")),
        abc_class=data.get("abc_class", "C"),
        active=safe_int(data.get("active", 1), 1),
    )
    if not fields["sku"] or not fields["name"]:
        return False, "SKU 与名称不能为空"
    with db.txn() as conn:
        if data.get("id"):
            dup = conn.execute("SELECT id FROM products WHERE sku=? AND id<>?",
                               (fields["sku"], data["id"])).fetchone()
            if dup:
                return False, "SKU 已存在"
            sets = ",".join(f"{k}=?" for k in fields)
            conn.execute(f"UPDATE products SET {sets} WHERE id=?", (*fields.values(), data["id"]))
        else:
            dup = conn.execute("SELECT id FROM products WHERE sku=?", (fields["sku"],)).fetchone()
            if dup:
                return False, "SKU 已存在"
            cols = ",".join(fields)
            marks = ",".join("?" * len(fields))
            conn.execute(f"INSERT INTO products({cols}) VALUES({marks})", tuple(fields.values()))
    return True, "已保存"


@_delete_guard
def delete_product(pid: int):
    """v1.5.1（P1-2）：补查 stock_moves（盘点调整也会产生流水）。"""
    with db.txn() as conn:
        used = conn.execute(
            "SELECT COUNT(*) c FROM (SELECT po_id id FROM purchase_items WHERE product_id=? "
            "UNION ALL SELECT so_id FROM sale_items WHERE product_id=?)", (pid, pid)
        ).fetchone()["c"]
        if used:
            raise db.OpAbort("该商品已被单据引用，建议改为停用而非删除")
        msg = _guard(conn, [
            ("SELECT COUNT(*) FROM stock_moves WHERE product_id=?", (pid,),
             "该商品有 {n} 条出入库流水（含盘点调整），删除会断裂库存追溯链，建议改为停用"),
            ("SELECT COUNT(*) FROM inventory WHERE product_id=? AND qty<>0", (pid,),
             "该商品仍有库存余额，建议改为停用"),
        ])
        if msg:
            raise db.OpAbort(msg)
        conn.execute("DELETE FROM inventory WHERE product_id=?", (pid,))
        conn.execute("DELETE FROM products WHERE id=?", (pid,))
    return True, "已删除"


def product_options(only_active=True):
    with db.txn() as conn:
        sql = "SELECT id, sku, name, sale_price, cost_price, unit FROM products"
        if only_active:
            sql += " WHERE active=1"
        sql += " ORDER BY sku"
        return db.rows_to_dicts(conn.execute(sql).fetchall())


# ============================================================ 往来单位
def list_partners(keyword: str = "", ptype: str = ""):
    sql = "SELECT * FROM partners"
    where, args = [], []
    if keyword:
        where.append("(code LIKE ? OR name LIKE ? OR contact LIKE ? OR phone LIKE ?)")
        args += [f"%{keyword}%"] * 4
    if ptype:
        where.append("(type=? OR type='BOTH')")
        args.append(ptype)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY code"
    with db.txn() as conn:
        return db.rows_to_dicts(conn.execute(sql, args).fetchall())


def get_partner(pid: int):
    with db.txn() as conn:
        row = conn.execute("SELECT * FROM partners WHERE id=?", (pid,)).fetchone()
        return dict(row) if row else None


def save_partner(data: dict):
    fields = dict(
        code=data.get("code", "").strip(),
        name=data.get("name", "").strip(),
        type=data.get("type", "CUSTOMER"),
        contact=data.get("contact", ""),
        phone=data.get("phone", ""),
        email=data.get("email", ""),
        address=data.get("address", ""),
        tax_no=data.get("tax_no", ""),
        remark=data.get("remark", ""),
        payment_terms_days=safe_int(data.get("payment_terms_days"), 30),
        credit_limit=safe_float(data.get("credit_limit")),
        active=safe_int(data.get("active", 1), 1),
    )
    if not fields["code"] or not fields["name"]:
        return False, "编码与名称不能为空"
    with db.txn() as conn:
        if data.get("id"):
            dup = conn.execute("SELECT id FROM partners WHERE code=? AND id<>?",
                               (fields["code"], data["id"])).fetchone()
            if dup:
                return False, "编码已存在"
            sets = ",".join(f"{k}=?" for k in fields)
            conn.execute(f"UPDATE partners SET {sets} WHERE id=?", (*fields.values(), data["id"]))
        else:
            dup = conn.execute("SELECT id FROM partners WHERE code=?", (fields["code"],)).fetchone()
            if dup:
                return False, "编码已存在"
            cols = ",".join(fields)
            conn.execute(f"INSERT INTO partners({cols}) VALUES({','.join('?' * len(fields))})",
                         tuple(fields.values()))
    return True, "已保存"


@_delete_guard
def delete_partner(pid: int):
    """v1.5.1（P1-2）：补查 payments（只收过款、没开过单的单位过去会被外键拦截并抛异常）。"""
    with db.txn() as conn:
        used = conn.execute(
            "SELECT (SELECT COUNT(*) FROM purchase_orders WHERE supplier_id=?)"
            " + (SELECT COUNT(*) FROM sale_orders WHERE customer_id=?)", (pid, pid)
        ).fetchone()[0]
        if used:
            raise db.OpAbort("该单位已有业务单据，建议停用而非删除")
        msg = _guard(conn, [
            ("SELECT COUNT(*) FROM payments WHERE partner_id=?", (pid,),
             "该单位有 {n} 条收付款记录，删除会导致资金流水缺失，建议停用而非删除"),
        ])
        if msg:
            raise db.OpAbort(msg)
        conn.execute("DELETE FROM partners WHERE id=?", (pid,))
    return True, "已删除"


def partner_options(ptype: str = ""):
    return list_partners(ptype=ptype)


def touch_all():
    """占位：主数据保存后统一更新时间戳"""
    return now()
