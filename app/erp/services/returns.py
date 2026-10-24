# -*- coding: utf-8 -*-
"""退货服务（v2.2.0）：销售退货 / 采购退货

设计要点
--------
- 两类退货共用一张 return_orders 表，用 rt_type 区分：
    SALE_RETURN     客户把货退回公司 → 入库「退货区」，并红冲该客户应收
    PURCHASE_RETURN 把货退给供应商   → 出库，并红冲该供应商应付
- 红冲机制：过账时把原单已执行数量（销售 delivered_qty / 采购 received_qty）
  扣减退货量。收付款 _allocate 以「已执行金额 − 已核销」算应收/应付，
  因此扣减已执行量即自动反向调整 AR/AP，无需额外记一笔收付款（避免重复冲减）。
- 库存联动走 inventory.apply_move（与采购收货/销售发货同一入口），
  分区账 Σzone_stock ≡ inventory 由该函数保证，退货区是真实台账库位。
- 状态机：DRAFT → CONFIRMED（审核）→ DONE（过账执行）；已 DONE 不可取消/重复过账。
"""
from .. import db
from ..utils import now, today, safe_float
from . import inventory as inv_svc
from . import zones as zone_svc
from . import sales as sale_svc
from . import purchase as po_svc

RT_SQL = """
SELECT r.*, pt.name AS partner_name, pt.code AS partner_code, w.name AS warehouse_name,
       COALESCE((SELECT so_no FROM sale_orders WHERE id=r.ref_id),
                (SELECT po_no FROM purchase_orders WHERE id=r.ref_id), '') AS ref_no,
       (SELECT COALESCE(SUM(qty),0) FROM return_items WHERE rto_id=r.id) AS total_qty
FROM return_orders r
JOIN partners pt ON pt.id=r.partner_id
JOIN warehouses w ON w.id=r.warehouse_id
"""


def _valid_type(rt_type: str) -> bool:
    return rt_type in ("SALE_RETURN", "PURCHASE_RETURN")


def _orig_line_table(rt_type: str) -> str:
    return "sale_items" if rt_type == "SALE_RETURN" else "purchase_items"


def _returned_qty(conn, rt_type: str, ref_line_id: int, exclude_rto_id=None) -> float:
    """某原单明细行已被（非取消）退货单退掉的总量"""
    sql = """
    SELECT COALESCE(SUM(ri.qty),0) q FROM return_items ri
    JOIN return_orders ro ON ro.id=ri.rto_id
    WHERE ri.ref_line_id=? AND ro.rt_type=? AND ro.status<>'CANCELLED'
    """
    args = [ref_line_id, rt_type]
    if exclude_rto_id:
        sql += " AND ro.id<>? "
        args.append(exclude_rto_id)
    return float(conn.execute(sql, args).fetchone()["q"])


def returnable_for_line(rt_type: str, ref_line_id: int, exclude_rto_id=None) -> float:
    with db.txn() as conn:
        return _returned_qty(conn, rt_type, ref_line_id, exclude_rto_id)


def list_returnable_lines(rt_type: str, ref_id: int) -> list:
    """取原单（已执行）明细行，并标注可退量，供新建/编辑退货单时预填"""
    tbl = _orig_line_table(rt_type)
    join = "sale_orders" if rt_type == "SALE_RETURN" else "purchase_orders"
    fk = "so_id" if rt_type == "SALE_RETURN" else "po_id"
    with db.txn() as conn:
        rows = conn.execute(
            f"SELECT i.*, p.sku, p.name, p.unit, p.cost_price,"
            f" (SELECT COALESCE(SUM(qty),0) FROM return_items ri"
            f"  JOIN return_orders ro ON ro.id=ri.rto_id"
            f"  WHERE ri.ref_line_id=i.id AND ro.rt_type=? AND ro.status<>'CANCELLED') AS returned_qty"
            f" FROM {tbl} i JOIN products p ON p.id=i.product_id"
            f" JOIN {join} o ON o.id=i.{fk}"
            f" WHERE i.{fk}=?", (rt_type, ref_id)).fetchall()
        out = []
        for r in rows:
            executed = float(r["delivered_qty"] if rt_type == "SALE_RETURN" else r["received_qty"])
            returned = float(r["returned_qty"])
            out.append({
                "ref_line_id": r["id"], "product_id": r["product_id"],
                "sku": r["sku"], "name": r["name"], "unit": r["unit"],
                "executed": executed, "returned": returned,
                "returnable": round(executed - returned, 6),
                "unit_price": float(r["unit_price"]),
            })
        return out


def list_orders(rt_type: str = "", keyword: str = "", status: str = "",
                date_from: str = "", date_to: str = "", limit: int = 500) -> list:
    where, args = [], []
    if rt_type:
        where.append("r.rt_type=?")
        args.append(rt_type)
    if keyword:
        where.append("(r.rt_no LIKE ? OR pt.name LIKE ? OR r.remark LIKE ?)")
        args += [f"%{keyword}%"] * 3
    if status:
        where.append("r.status=?")
        args.append(status)
    if date_from:
        where.append("r.order_date >= ?")
        args.append(date_from)
    if date_to:
        where.append("r.order_date <= ?")
        args.append(date_to)
    sql = RT_SQL
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY r.id DESC LIMIT ?"
    args.append(limit)
    with db.txn() as conn:
        return db.rows_to_dicts(conn.execute(sql, args).fetchall())


def get_order(roid: int) -> dict:
    with db.txn() as conn:
        row = conn.execute(RT_SQL + " WHERE r.id=?", (roid,)).fetchone()
        if not row:
            return None
        order = dict(row)
        order["items"] = db.rows_to_dicts(
            conn.execute(
                "SELECT ri.*, p.sku, p.name AS product_name, p.unit, p.spec"
                " FROM return_items ri JOIN products p ON p.id=ri.product_id"
                " WHERE ri.rto_id=? ORDER BY ri.sort_no, ri.id", (roid,)).fetchall())
        return order


def _load_orig(conn, rt_type: str, ref_id: int):
    """校验关联原单存在且类型匹配，返回 (partner_id, warehouse_id)"""
    if rt_type == "SALE_RETURN":
        o = conn.execute("SELECT id, customer_id, warehouse_id FROM sale_orders WHERE id=?",
                         (ref_id,)).fetchone()
        if not o:
            raise db.OpAbort("关联的销售订单不存在")
        return int(o["customer_id"]), int(o["warehouse_id"])
    else:
        o = conn.execute("SELECT id, supplier_id, warehouse_id FROM purchase_orders WHERE id=?",
                         (ref_id,)).fetchone()
        if not o:
            raise db.OpAbort("关联的采购订单不存在")
        return int(o["supplier_id"]), int(o["warehouse_id"])


def save_order(header: dict, items: list, user: dict = None) -> tuple:
    """新建或更新退货单（仅 DRAFT 可改）。

    header: {rt_type, ref_id, warehouse_id?, order_date, remark, id?(编辑时)}
    items:  [{product_id, ref_line_id, qty, unit_price}]
    返回 (ok, msg/id)
    """
    rt_type = header.get("rt_type")
    if not _valid_type(rt_type):
        return False, "退货类型无效"
    ref_id = safe_float(header.get("ref_id") or 0)
    if ref_id <= 0:
        return False, "请选择关联的原单（销售单/采购单）"
    if not items:
        return False, "请至少填写一条退货明细"

    cleaned = []
    for idx, it in enumerate(items, start=1):
        pid = int(safe_float(it.get("product_id") or 0))
        lid = int(safe_float(it.get("ref_line_id") or 0))
        qty = safe_float(it.get("qty"))
        price = safe_float(it.get("unit_price"))
        if pid <= 0 or lid <= 0:
            return False, f"第 {idx} 行商品或关联明细缺失"
        if qty <= 0:
            return False, f"第 {idx} 行退货数量必须大于 0"
        if price < 0:
            return False, f"第 {idx} 行退货单价不能为负"
        cleaned.append({"product_id": pid, "ref_line_id": lid, "qty": qty, "unit_price": price})

    rto_id = header.get("id")
    with db.txn() as conn:
        if rto_id:
            cur = conn.execute("SELECT status FROM return_orders WHERE id=?", (rto_id,)).fetchone()
            if not cur:
                return False, "退货单不存在"
            if cur["status"] != "DRAFT":
                return False, "仅草稿状态可编辑"
        partner_id, wh_id = _load_orig(conn, rt_type, int(ref_id))
        # 校验每行退货量不超过可退
        for it in cleaned:
            executed = float(conn.execute(
                f"SELECT COALESCE({'delivered_qty' if rt_type=='SALE_RETURN' else 'received_qty'},0)"
                f" FROM {_orig_line_table(rt_type)} WHERE id=?", (it["ref_line_id"],)).fetchone()[0])
            returned = _returned_qty(conn, rt_type, it["ref_line_id"], rto_id)
            if it["qty"] - (executed - returned) > 1e-6:
                raise db.OpAbort(
                    f"退货数量超过可退量（已执行 {executed:g}，已退 {returned:g}）")
        total = round(sum(it["qty"] * it["unit_price"] for it in cleaned), 4)
        order_date = header.get("order_date") or today()
        created = (user or {}).get("username", "")
        if rto_id:
            conn.execute(
                "UPDATE return_orders SET rt_type=?, ref_type=?, ref_id=?, partner_id=?,"
                " warehouse_id=?, order_date=?, total_amount=?, remark=? WHERE id=?",
                (rt_type, "SALE" if rt_type == "SALE_RETURN" else "PURCHASE", int(ref_id),
                 partner_id, wh_id, order_date, total, header.get("remark", ""), rto_id))
            conn.execute("DELETE FROM return_items WHERE rto_id=?", (rto_id,))
        else:
            rt_no = db.next_no(conn, "RETURN", "RT")
            conn.execute(
                "INSERT INTO return_orders(rt_no, rt_type, ref_type, ref_id, partner_id,"
                " warehouse_id, order_date, status, total_amount, remark, created_by)"
                " VALUES(?,?,?,?,?,?,?, 'DRAFT', ?,?,?)",
                (rt_no, rt_type, "SALE" if rt_type == "SALE_RETURN" else "PURCHASE",
                 int(ref_id), partner_id, wh_id, order_date, total,
                 header.get("remark", ""), created))
            rto_id = conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        for i, it in enumerate(cleaned, start=1):
            conn.execute(
                "INSERT INTO return_items(rto_id, product_id, ref_line_id, qty, unit_price, amount, sort_no)"
                " VALUES(?,?,?,?,?,?,?)",
                (rto_id, it["product_id"], it["ref_line_id"], it["qty"], it["unit_price"],
                 round(it["qty"] * it["unit_price"], 4), i))
        from .auth import log
        log(conn, (user or {}).get("id"), created, "保存退货单", "退货",
            f"#{rto_id}", f"类型 {rt_type}，金额 {total:,.2f}")
    return True, rto_id


def confirm_order(roid: int, user: dict = None) -> tuple:
    with db.txn() as conn:
        o = conn.execute("SELECT * FROM return_orders WHERE id=?", (roid,)).fetchone()
        if not o:
            return False, "退货单不存在"
        if o["status"] != "DRAFT":
            return False, "仅草稿状态可确认"
        conn.execute("UPDATE return_orders SET status='CONFIRMED' WHERE id=?", (roid,))
        from .auth import log
        log(conn, (user or {}).get("id"), (user or {}).get("username", ""), "确认退货单", "退货",
            o["rt_no"], "已审核")
    return True, f"已确认：{o['rt_no']}"


def post_return(roid: int, user: dict = None, move_date: str = None) -> tuple:
    """过账执行：库存联动 + 红冲原单应收/应付。幂等：已 DONE 直接返回。"""
    with db.txn() as conn:
        o = conn.execute("SELECT * FROM return_orders WHERE id=?", (roid,)).fetchone()
        if not o:
            return False, "退货单不存在"
        if o["status"] == "DONE":
            return True, f"已执行过，无需重复过账：{o['rt_no']}"
        if o["status"] == "CANCELLED":
            return False, "已取消的退货单不能过账"
        rt_type = o["rt_type"]
        rt_no = o["rt_no"]
        wh_id = int(o["warehouse_id"])
        items = conn.execute(
            "SELECT * FROM return_items WHERE rto_id=? ORDER BY sort_no, id", (roid,)).fetchall()
        if not items:
            return False, "退货单没有明细"
        md = move_date or today()

        if rt_type == "SALE_RETURN":
            # 入库到退货区（真实台账库位）
            ret_zone = next((z["id"] for z in zone_svc.list_zones(wh_id)
                             if z["zone_type"] == "RETURN"), None)
            for it in items:
                qty = float(it["qty"])
                # 退货入库成本取原销售出库时记录的成本，保证库存加权成本不被污染
                line = conn.execute(
                    "SELECT unit_cost, product_id FROM sale_items WHERE id=?",
                    (it["ref_line_id"],)).fetchone()
                prod = conn.execute("SELECT cost_price FROM products WHERE id=?",
                                    (it["product_id"],)).fetchone()
                cost = float(line["unit_cost"]) if line and float(line["unit_cost"] or 0) > 0 \
                    else float(prod["cost_price"]) if prod else 0.0
                inv_svc.apply_move(conn, it["product_id"], wh_id, 1, qty, cost,
                                   "IN", "RETURN", rt_no, roid, "销售退货入库",
                                   user, md, zone_id=ret_zone)
                # 红冲应收：扣减原销售单已出库量
                conn.execute("UPDATE sale_items SET delivered_qty=delivered_qty-? WHERE id=?",
                             (qty, it["ref_line_id"]))
                so_id = conn.execute("SELECT so_id FROM sale_items WHERE id=?",
                                     (it["ref_line_id"],)).fetchone()["so_id"]
                sale_svc._sync_status(conn, so_id)
        else:  # PURCHASE_RETURN：出库（默认存货区），红冲应付
            for it in items:
                qty = float(it["qty"])
                line = conn.execute(
                    "SELECT unit_price, product_id FROM purchase_items WHERE id=?",
                    (it["ref_line_id"],)).fetchone()
                cost = float(line["unit_price"]) if line else 0.0
                # apply_move 不足会抛 ValueError → 转 OpAbort 回滚
                try:
                    inv_svc.apply_move(conn, it["product_id"], wh_id, -1, qty, cost,
                                       "OUT", "RETURN", rt_no, roid, "采购退货出库",
                                       user, md)
                except ValueError as e:
                    raise db.OpAbort(str(e))
                conn.execute("UPDATE purchase_items SET received_qty=received_qty-? WHERE id=?",
                             (qty, it["ref_line_id"]))
                po_id = conn.execute("SELECT po_id FROM purchase_items WHERE id=?",
                                     (it["ref_line_id"],)).fetchone()["po_id"]
                po_svc._sync_status(conn, po_id)

        conn.execute("UPDATE return_orders SET status='DONE', total_amount=? WHERE id=?",
                     (float(o["total_amount"]), roid))
        from .auth import log
        log(conn, (user or {}).get("id"), (user or {}).get("username", ""), "过账退货单", "退货",
            rt_no, f"类型 {rt_type}，金额 {float(o['total_amount']):,.2f}")
    return True, f"已过账执行：{rt_no}"


def cancel_order(roid: int, user: dict = None) -> tuple:
    with db.txn() as conn:
        o = conn.execute("SELECT * FROM return_orders WHERE id=?", (roid,)).fetchone()
        if not o:
            return False, "退货单不存在"
        if o["status"] == "DONE":
            return False, "已执行（过账）的退货单不能取消，如需冲正请另开一张反向退货单"
        if o["status"] == "CANCELLED":
            return True, "已是取消状态"
        conn.execute("UPDATE return_orders SET status='CANCELLED' WHERE id=?", (roid,))
        from .auth import log
        log(conn, (user or {}).get("id"), (user or {}).get("username", ""), "取消退货单", "退货",
            o["rt_no"], "已取消（未过账，库存与应收应付不变）")
    return True, f"已取消：{o['rt_no']}"


def delete_order(roid: int, user: dict = None) -> tuple:
    """仅草稿可删除（连带明细）"""
    with db.txn() as conn:
        o = conn.execute("SELECT status FROM return_orders WHERE id=?", (roid,)).fetchone()
        if not o:
            return False, "退货单不存在"
        if o["status"] != "DRAFT":
            return False, "仅草稿状态可删除"
        conn.execute("DELETE FROM return_orders WHERE id=?", (roid,))
        from .auth import log
        log(conn, (user or {}).get("id"), (user or {}).get("username", ""), "删除退货单", "退货",
            f"#{roid}", "草稿删除")
    return True, "已删除"
