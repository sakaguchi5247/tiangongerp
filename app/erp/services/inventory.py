# -*- coding: utf-8 -*-
"""库存服务：移动加权平均成本核算、出入库流水、盘点调整、预警"""
from .. import db
from ..utils import now, today, safe_float, div
from . import zones


def _ensure_row(conn, product_id: int, warehouse_id: int):
    conn.execute("INSERT OR IGNORE INTO inventory(product_id, warehouse_id, qty, avg_cost) VALUES(?,?,0,0)",
                 (product_id, warehouse_id))
    return conn.execute("SELECT * FROM inventory WHERE product_id=? AND warehouse_id=?",
                        (product_id, warehouse_id)).fetchone()


def current_stock(conn, product_id: int, warehouse_id: int) -> dict:
    r = _ensure_row(conn, product_id, warehouse_id)
    return dict(r)


def total_stock(conn, product_id: int) -> float:
    r = conn.execute("SELECT COALESCE(SUM(qty),0) q FROM inventory WHERE product_id=?",
                     (product_id,)).fetchone()
    return float(r["q"])


def apply_move(conn, product_id: int, warehouse_id: int, direction: int, qty: float,
               unit_cost: float = None, move_type: str = "IN", ref_type: str = "",
               ref_no: str = "", ref_id: int = None, remark: str = "",
               user: dict = None, move_date: str = None, zone_id: int = None) -> dict:
    """核心库存过账：写入流水 + 更新库存余额与移动加权平均成本

    direction: +1 入库 / -1 出库
    zone_id: 入库落到的库区；省略则落到该仓库默认「存货区」（v1.6.0 分区可视化）。
    库存余额（inventory）与库区分账（zone_stock）在本函数内同写，保证分区之和≡仓库总量。
    """
    qty = abs(safe_float(qty))
    if qty == 0:
        raise ValueError("数量必须大于 0")

    # 解析目标库区：仅入库（direction>0）时把数量记到指定/默认区；出库从指定/默认区扣减
    if zone_id is None:
        zid = zones.default_zone_id(conn, warehouse_id)
    else:
        zid = int(zone_id)

    row = _ensure_row(conn, product_id, warehouse_id)
    old_qty = float(row["qty"])
    old_avg = float(row["avg_cost"])

    if unit_cost is None or safe_float(unit_cost) == 0:
        prod = conn.execute("SELECT cost_price FROM products WHERE id=?", (product_id,)).fetchone()
        unit_cost = float(prod["cost_price"]) if prod else 0.0
    unit_cost = safe_float(unit_cost)

    if direction > 0:                                   # ---- 入库：更新加权成本
        new_qty = old_qty + qty
        if new_qty > 0:
            new_avg = (old_qty * old_avg + qty * unit_cost) / new_qty
        else:
            new_avg = unit_cost
    else:                                               # ---- 出库：成本不变
        if old_qty - qty < -1e-9:
            raise ValueError(f"库存不足：当前库存 {old_qty:g}，本次出库 {qty:g}")
        new_qty = old_qty - qty
        new_avg = old_avg if new_qty > 0 else old_avg
        if abs(unit_cost) < 1e-9:
            unit_cost = old_avg

    move_no = db.next_no(conn, "STOCK", "ST")
    md = move_date or today()
    amount = round(qty * unit_cost, 4)
    conn.execute(
        "INSERT INTO stock_moves(move_no, move_date, move_type, direction, product_id, warehouse_id,"
        " qty, unit_cost, amount, balance_after, zone_id, ref_type, ref_no, ref_id, remark, created_by, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (move_no, md, move_type, int(direction), product_id, warehouse_id, qty, unit_cost,
         amount, new_qty, zid, ref_type, ref_no, ref_id, remark,
         (user or {}).get("username", ""), now()),
    )
    conn.execute("UPDATE inventory SET qty=?, avg_cost=?, updated_at=? WHERE product_id=? AND warehouse_id=?",
                 (new_qty, new_avg, now(), product_id, warehouse_id))
    # 同步库区分账：入库加、出库减（默认/指定区）
    conn.execute(
        "INSERT OR IGNORE INTO zone_stock(product_id, warehouse_id, zone_id, qty) VALUES(?,?,?,0)",
        (product_id, warehouse_id, zid))
    conn.execute(
        "UPDATE zone_stock SET qty = qty + ? WHERE product_id=? AND warehouse_id=? AND zone_id=?",
        (direction * qty, product_id, warehouse_id, zid))
    conn.execute(
        "DELETE FROM zone_stock WHERE product_id=? AND warehouse_id=? AND zone_id=? AND qty=0",
        (product_id, warehouse_id, zid))
    return {"move_no": move_no, "balance_after": new_qty, "unit_cost": unit_cost, "amount": amount}


def adjust_stock(product_id: int, warehouse_id: int, target_qty: float, remark: str,
                 user: dict, unit_cost: float = None) -> dict:
    """盘点调整：把库存调整到 target_qty

    v1.5.1（P2-10）：过去库存不足时向外抛裸 ValueError，界面只能显示一条
    带着异常类型的英文报错。现统一改抛 db.OpAbort（与出库发货一致），
    由调用方转成一句中文提示；数量本身也先做合法性校验。
    """
    target_qty = safe_float(target_qty)
    if target_qty < 0:
        raise db.OpAbort("盘点数量不能为负")
    if unit_cost is not None and safe_float(unit_cost) < 0:
        raise db.OpAbort("单位成本不能为负")
    try:
        with db.txn() as conn:
            row = _ensure_row(conn, product_id, warehouse_id)
            diff = safe_float(target_qty) - float(row["qty"])
            if abs(diff) < 1e-9:
                return {"changed": False, "diff": 0}
            if diff > 0:
                res = apply_move(conn, product_id, warehouse_id, 1, diff,
                                 unit_cost or float(row["avg_cost"]),
                                 "ADJ", "ADJUST", "", None, remark or "盘点盘盈", user)
            else:
                res = apply_move(conn, product_id, warehouse_id, -1, -diff, None,
                                 "ADJ", "ADJUST", "", None, remark or "盘点盘亏", user)
            from .auth import log
            log(conn, (user or {}).get("id"), (user or {}).get("username", ""), "库存调整", "库存",
                res["move_no"], f"调整数量 {diff:g}，调整后 {res['balance_after']:g}")
            res["changed"] = True
            res["diff"] = diff
            return res
    except ValueError as e:                     # 来自 apply_move 的「库存不足」
        raise db.OpAbort(str(e))


# ============================================================ 查询
STOCK_SQL = """
SELECT i.product_id, i.warehouse_id, i.qty, i.avg_cost, i.updated_at,
       p.sku, p.name, p.spec, p.unit, p.safety_stock, p.reorder_point, p.lead_time_days,
       p.sale_price, p.cost_price, p.abc_class,
       c.name AS category_name, w.name AS warehouse_name,
       ROUND(i.qty*i.avg_cost, 2) AS stock_value,
       ROUND(i.qty*p.sale_price, 2) AS retail_value
FROM inventory i
JOIN products p ON p.id=i.product_id
LEFT JOIN categories c ON c.id=p.category_id
JOIN warehouses w ON w.id=i.warehouse_id
"""


def list_stock(keyword: str = "", warehouse_id=None, category_id=None,
               only_low=False, only_positive=False):
    where, args = [], []
    if keyword:
        where.append("(p.sku LIKE ? OR p.name LIKE ? OR p.spec LIKE ?)")
        args += [f"%{keyword}%"] * 3
    if warehouse_id:
        where.append("i.warehouse_id=?")
        args.append(warehouse_id)
    if category_id:
        where.append("p.category_id=?")
        args.append(category_id)
    if only_low:
        where.append("i.qty <= p.safety_stock")
    if only_positive:
        where.append("i.qty <> 0")
    sql = STOCK_SQL
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY p.sku, w.code"
    with db.txn() as conn:
        return db.rows_to_dicts(conn.execute(sql, args).fetchall())


def list_moves(keyword: str = "", product_id=None, warehouse_id=None, move_type: str = "",
               date_from: str = "", date_to: str = "", limit: int = 500):
    sql = """
    SELECT m.*, p.sku, p.name AS product_name, p.unit, w.name AS warehouse_name
    FROM stock_moves m
    JOIN products p ON p.id=m.product_id
    JOIN warehouses w ON w.id=m.warehouse_id
    """
    where, args = [], []
    if keyword:
        where.append("(p.sku LIKE ? OR p.name LIKE ? OR m.ref_no LIKE ? OR m.move_no LIKE ?)")
        args += [f"%{keyword}%"] * 4
    if product_id:
        where.append("m.product_id=?")
        args.append(product_id)
    if warehouse_id:
        where.append("m.warehouse_id=?")
        args.append(warehouse_id)
    if move_type:
        where.append("m.move_type=?")
        args.append(move_type)
    if date_from:
        where.append("m.move_date >= ?")
        args.append(date_from)
    if date_to:
        where.append("m.move_date <= ?")
        args.append(date_to)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY m.id DESC LIMIT ?"
    args.append(limit)
    with db.txn() as conn:
        return db.rows_to_dicts(conn.execute(sql, args).fetchall())


def low_stock_alerts():
    """低于安全库存的 SKU 清单（按缺货缺口排序）"""
    sql = STOCK_SQL + """
     WHERE i.qty <= p.safety_stock AND p.active=1
     ORDER BY (p.safety_stock - i.qty) DESC
    """
    with db.txn() as conn:
        return db.rows_to_dicts(conn.execute(sql).fetchall())


def stock_summary():
    with db.txn() as conn:
        row = conn.execute(
            "SELECT COUNT(DISTINCT product_id) skus, COALESCE(SUM(qty),0) qty,"
            " COALESCE(SUM(qty*avg_cost),0) value FROM inventory"
        ).fetchone()
        return dict(row)


def recalc_reorder_points(user: dict = None, window: int = 90) -> int:
    """重算再订货点 ROP = 日均需求 × 提前期 + 安全库存"""
    with db.txn() as conn:
        rows = conn.execute(
            """SELECT p.id, p.lead_time_days, p.safety_stock,
                      COALESCE((SELECT SUM(m.qty) FROM stock_moves m
                                WHERE m.product_id=p.id AND m.direction=-1
                                  AND m.move_date >= date('now',?)),0) AS out_qty
               FROM products p WHERE p.active=1""",
            (f"-{int(window)} day",),
        ).fetchall()
        n = 0
        for r in rows:
            daily = div(r["out_qty"], window)
            rop = round(daily * (r["lead_time_days"] or 7) + (r["safety_stock"] or 0), 2)
            conn.execute("UPDATE products SET reorder_point=? WHERE id=?", (rop, r["id"]))
            n += 1
        if user:
            from .auth import log
            log(conn, user.get("id"), user.get("username", ""), "重算再订货点", "库存", "",
                f"共 {n} 个商品，窗口 {window} 天")
        return n
