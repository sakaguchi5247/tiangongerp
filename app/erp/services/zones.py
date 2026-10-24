# -*- coding: utf-8 -*-
"""仓库分区服务（v1.6.0 仓库可视化）

设计要点
--------
- ``inventory`` 仍作为「仓库级」权威总量；``zone_stock`` 是「库区分账」。
- 两者在同一个过账函数内一起写（见 inventory.apply_move），因此
  Σ(zone_stock) ≡ inventory ≡ 进销存台账 是**构造性成立**的，永远不会对账穿帮。
- 库内移库（transfer_zone）只改 zone_stock、不动 inventory，用于把存货移入退货区/残次区等，
  让「退货区」是真实的台账库位，而非显示层瞎切。
"""
from .. import db
from ..utils import now, today, safe_float

ZONE_TYPES = {
    "STORAGE": ("存货区", "#2563EB"),
    "RETURN": ("退货区", "#DC2626"),
    "INSPECT": ("待检区", "#D97706"),
    "DEFECT": ("残次区", "#7C3AED"),
    "STAGING": ("暂存区", "#0891B2"),
}


def type_label(zone_type: str) -> str:
    return ZONE_TYPES.get(zone_type, (zone_type, "#64748B"))[0]


def type_color(zone_type: str) -> str:
    return ZONE_TYPES.get(zone_type, (None, "#64748B"))[1]


# ============================================================ 库区档案
def list_zones(warehouse_id=None):
    sql = "SELECT z.*, w.name AS warehouse_name FROM zones z JOIN warehouses w ON w.id=z.warehouse_id"
    args = []
    if warehouse_id:
        sql += " WHERE z.warehouse_id=?"
        args.append(warehouse_id)
    sql += " ORDER BY z.warehouse_id, z.sort_order, z.code"
    with db.txn() as conn:
        return db.rows_to_dicts(conn.execute(sql, args).fetchall())


def get_zone(zone_id: int) -> dict:
    with db.txn() as conn:
        r = conn.execute("SELECT * FROM zones WHERE id=?", (zone_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        # 与 zone_summary 保持一致，直接带出可读标签，避免调用方误取原始列缺失的键
        d["type_label"] = type_label(d["zone_type"])
        return d


def default_zone_id(conn, warehouse_id: int) -> int:
    """仓库的默认存货区（库存记账默认落此处）"""
    r = conn.execute(
        "SELECT id FROM zones WHERE warehouse_id=? AND zone_type='STORAGE' ORDER BY id LIMIT 1",
        (warehouse_id,)).fetchone()
    if not r:
        r = conn.execute(
            "SELECT id FROM zones WHERE warehouse_id=? ORDER BY sort_order LIMIT 1",
            (warehouse_id,)).fetchone()
    return int(r["id"]) if r else None


def default_zone_id_from_svc(warehouse_id: int):
    """不需要外部事务连接时的便捷入口：查仓库默认存货区 id"""
    with db.txn() as conn:
        return default_zone_id(conn, warehouse_id)


def save_zone(f: dict) -> tuple:
    """新建/更新库区。f 含 warehouse_id, code, name, zone_type, capacity, color, id(可选)"""
    wid = int(f.get("warehouse_id"))
    code = (f.get("code") or "").strip()
    name = (f.get("name") or "").strip()
    if not code or not name:
        return False, "库区编码与名称不能为空"
    ztype = f.get("zone_type") or "STORAGE"
    cap = safe_float(f.get("capacity"))
    color = (f.get("color") or "").strip()
    if not color:
        color = type_color(ztype)
    zid = f.get("id")
    with db.txn() as conn:
        if zid and str(zid).isdigit():
            conn.execute(
                "UPDATE zones SET code=?, name=?, zone_type=?, capacity=?, color=? WHERE id=?",
                (code, name, ztype, cap, color, int(zid)))
        else:
            exist = conn.execute(
                "SELECT id FROM zones WHERE warehouse_id=? AND code=?", (wid, code)).fetchone()
            if exist:
                return False, f"该仓库已存在编码为 {code} 的库区"
            conn.execute(
                "INSERT INTO zones(warehouse_id, code, name, zone_type, capacity, color, sort_order)"
                " VALUES(?,?,?,?,?,?, (SELECT COALESCE(MAX(sort_order),0)+10 FROM zones WHERE warehouse_id=?))",
                (wid, code, name, ztype, cap, color, wid))
        from .auth import log
        log(conn, (f.get("user") or {}).get("id"), (f.get("user") or {}).get("username", ""),
            "维护库区", "库存", "", f"{name}（{code}）")
    return True, "已保存"


# ============================================================ 分区库存
def current_zone_stock(conn, product_id: int, warehouse_id: int, zone_id: int) -> float:
    r = conn.execute(
        "SELECT COALESCE(qty,0) q FROM zone_stock WHERE product_id=? AND warehouse_id=? AND zone_id=?",
        (product_id, warehouse_id, zone_id)).fetchone()
    return float(r["q"]) if r else 0.0


def zone_summary(warehouse_id: int) -> list:
    """每个库区的汇总：数量、SKU 数、库存金额、占用率"""
    sql = """
    SELECT z.id, z.code, z.name, z.zone_type, z.capacity, z.color,
           COALESCE(SUM(zs.qty),0) AS qty,
           COUNT(DISTINCT zs.product_id) AS skus,
           COALESCE(SUM(zs.qty * i.avg_cost),0) AS value
    FROM zones z
    LEFT JOIN zone_stock zs ON zs.zone_id = z.id
    LEFT JOIN inventory i ON i.product_id=zs.product_id AND i.warehouse_id=zs.warehouse_id
    WHERE z.warehouse_id=?
    GROUP BY z.id
    ORDER BY z.sort_order, z.code
    """
    with db.txn() as conn:
        rows = db.rows_to_dicts(conn.execute(sql, (warehouse_id,)).fetchall())
    for r in rows:
        cap = float(r["capacity"] or 0)
        r["occupancy"] = (float(r["qty"]) / cap) if cap > 0 else None
        r["type_label"] = type_label(r["zone_type"])
    return rows


def warehouse_totals(warehouse_id: int) -> dict:
    with db.txn() as conn:
        inv = conn.execute(
            "SELECT COALESCE(SUM(qty),0) q, COALESCE(SUM(qty*avg_cost),0) v "
            "FROM inventory WHERE warehouse_id=?", (warehouse_id,)).fetchone()
        zq = conn.execute(
            "SELECT COALESCE(SUM(qty),0) q FROM zone_stock zs "
            "JOIN zones z ON z.id=zs.zone_id WHERE z.warehouse_id=?",
            (warehouse_id,)).fetchone()
    inv_q = float(inv["q"]); zq_q = float(zq["q"])
    return {
        "inv_qty": inv_q, "inv_value": float(inv["v"]),
        "zone_qty": zq_q,
        "match": abs(inv_q - zq_q) < 1e-6,
        "diff": round(inv_q - zq_q, 4),
    }


def reconcile(conn, warehouse_id=None) -> list:
    """逐仓库逐商品核对 Σzone_stock 与 inventory，返回不一致清单（正常应为空）"""
    where = "WHERE i.warehouse_id=?" if warehouse_id else ""
    args = (warehouse_id,) if warehouse_id else ()
    sql = f"""
    SELECT i.warehouse_id, i.product_id, i.qty AS inv_qty,
           COALESCE(z.zty,0) AS zone_qty
    FROM inventory i
    LEFT JOIN (
        SELECT product_id, warehouse_id, SUM(qty) zty FROM zone_stock GROUP BY product_id, warehouse_id
    ) z ON z.product_id=i.product_id AND z.warehouse_id=i.warehouse_id
    {where}
    """
    rows = db.rows_to_dicts(conn.execute(sql, args).fetchall())
    bad = []
    for r in rows:
        if abs(float(r["inv_qty"]) - float(r["zone_qty"])) > 1e-6:
            bad.append(r)
    return bad


def zone_detail(zone_id: int, keyword: str = "") -> list:
    """某库区内的 SKU 明细（钻取下探用）"""
    sql = """
    SELECT zs.product_id, zs.qty, p.sku, p.name, p.spec, p.unit,
           i.avg_cost, ROUND(zs.qty*i.avg_cost,2) AS value
    FROM zone_stock zs
    JOIN products p ON p.id=zs.product_id
    LEFT JOIN inventory i ON i.product_id=zs.product_id AND i.warehouse_id=(
        SELECT warehouse_id FROM zones WHERE id=?)
    WHERE zs.zone_id=? AND zs.qty<>0
    """
    args = [zone_id, zone_id]
    if keyword:
        sql += " AND (p.sku LIKE ? OR p.name LIKE ? OR p.spec LIKE ?)"
        args += [f"%{keyword}%"] * 3
    sql += " ORDER BY value DESC"
    with db.txn() as conn:
        return db.rows_to_dicts(conn.execute(sql, args).fetchall())


# ============================================================ 库内移库
def _add_zone(conn, product_id, warehouse_id, zone_id, delta):
    conn.execute(
        "INSERT OR IGNORE INTO zone_stock(product_id, warehouse_id, zone_id, qty) VALUES(?,?,?,0)",
        (product_id, warehouse_id, zone_id))
    conn.execute(
        "UPDATE zone_stock SET qty = qty + ? WHERE product_id=? AND warehouse_id=? AND zone_id=?",
        (delta, product_id, warehouse_id, zone_id))
    conn.execute("DELETE FROM zone_stock WHERE product_id=? AND warehouse_id=? AND zone_id=? AND qty=0",
                 (product_id, warehouse_id, zone_id))


def transfer_zone(product_id: int, warehouse_id: int, from_zone: int, to_zone: int,
                  qty: float, user: dict = None, move_date: str = None, remark: str = "") -> tuple:
    """库内移库：把某商品从 from_zone 移到 to_zone（同仓库）。

    inventory 总量不变，仅 zone_stock 重分类；写两条 TRANSFER 流水（带 zone_id）以便追溯。
    移库后仍满足 Σzone_stock ≡ inventory，reconcile 不受影响。
    """
    qty = safe_float(qty)
    if qty <= 0:
        return False, "移库数量必须大于 0"
    if from_zone == to_zone:
        return False, "源库区与目标库区不能相同"
    with db.txn() as conn:
        have = current_zone_stock(conn, product_id, warehouse_id, from_zone)
        if have - qty < -1e-9:
            return False, f"源库区库存不足：当前 {have:g}，本次移出 {qty:g}"
        prod = conn.execute("SELECT cost_price FROM products WHERE id=?", (product_id,)).fetchone()
        unit_cost = float(prod["cost_price"]) if prod else 0.0
        md = move_date or today()
        # 出库腿（源库区）
        no1 = db.next_no(conn, "STOCK", "ST")
        conn.execute(
            "INSERT INTO stock_moves(move_no, move_date, move_type, direction, product_id, warehouse_id,"
            " qty, unit_cost, amount, balance_after, zone_id, ref_type, ref_no, ref_id, remark, created_by, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (no1, md, "TRANSFER", -1, product_id, warehouse_id, qty, unit_cost,
             round(qty * unit_cost, 4), have - qty, from_zone, "TRANSFER", no1, None,
             remark or "库内移库-出", (user or {}).get("username", ""), now()))
        # 入库腿（目标库区）
        to_qty = current_zone_stock(conn, product_id, warehouse_id, to_zone)
        no2 = db.next_no(conn, "STOCK", "ST")
        conn.execute(
            "INSERT INTO stock_moves(move_no, move_date, move_type, direction, product_id, warehouse_id,"
            " qty, unit_cost, amount, balance_after, zone_id, ref_type, ref_no, ref_id, remark, created_by, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (no2, md, "TRANSFER", 1, product_id, warehouse_id, qty, unit_cost,
             round(qty * unit_cost, 4), to_qty + qty, to_zone, "TRANSFER", no2, None,
             remark or "库内移库-入", (user or {}).get("username", ""), now()))
        _add_zone(conn, product_id, warehouse_id, from_zone, -qty)
        _add_zone(conn, product_id, warehouse_id, to_zone, qty)
        # 对账校验：移库不得破坏分区一致性
        bad = reconcile(conn, warehouse_id)
        if bad:
            raise db.OpAbort("移库后分区对账异常，已回滚")
        from .auth import log
        log(conn, (user or {}).get("id"), (user or {}).get("username", ""), "库内移库", "库存",
            no1, f"商品#{product_id} 从库区{from_zone}→{to_zone} {qty:g}")
    return True, "移库成功"
