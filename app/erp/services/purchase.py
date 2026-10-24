# -*- coding: utf-8 -*-
"""采购服务：采购订单 CRUD + 确认 + 收货入库 + 状态机"""
from .. import db
from ..utils import now, today, safe_float, safe_int, check_date
from . import inventory as inv_svc

ORDER_SQL = """
SELECT o.*, pt.name AS partner_name, pt.code AS partner_code, w.name AS warehouse_name,
       (SELECT COALESCE(SUM(qty),0) FROM purchase_items WHERE po_id=o.id) AS total_qty,
       (SELECT COALESCE(SUM(received_qty),0) FROM purchase_items WHERE po_id=o.id) AS recv_qty
FROM purchase_orders o
JOIN partners pt ON pt.id=o.supplier_id
JOIN warehouses w ON w.id=o.warehouse_id
"""

ITEM_SQL = """
SELECT i.*, p.sku, p.name AS product_name, p.unit, p.spec
FROM purchase_items i JOIN products p ON p.id=i.product_id
"""


def list_orders(keyword="", supplier_id=None, status="", date_from="", date_to="", limit=500):
    where, args = [], []
    if keyword:
        where.append("(o.po_no LIKE ? OR pt.name LIKE ? OR o.remark LIKE ?)")
        args += [f"%{keyword}%"] * 3
    if supplier_id:
        where.append("o.supplier_id=?")
        args.append(supplier_id)
    if status:
        where.append("o.status=?")
        args.append(status)
    if date_from:
        where.append("o.order_date >= ?")
        args.append(date_from)
    if date_to:
        where.append("o.order_date <= ?")
        args.append(date_to)
    sql = ORDER_SQL
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY o.id DESC LIMIT ?"
    args.append(limit)
    with db.txn() as conn:
        return db.rows_to_dicts(conn.execute(sql, args).fetchall())


def get_order(oid: int):
    with db.txn() as conn:
        row = conn.execute(ORDER_SQL + " WHERE o.id=?", (oid,)).fetchone()
        if not row:
            return None
        order = dict(row)
        order["items"] = db.rows_to_dicts(
            conn.execute(ITEM_SQL + " WHERE i.po_id=? ORDER BY i.sort_no, i.id", (oid,)).fetchall()
        )
        return order


def _recalc(conn, oid: int):
    row = conn.execute("SELECT COALESCE(SUM(amount),0) t FROM purchase_items WHERE po_id=?",
                       (oid,)).fetchone()
    conn.execute("UPDATE purchase_orders SET total_amount=? WHERE id=?", (row["t"], oid))


def _sync_status(conn, oid: int):
    row = conn.execute(
        "SELECT COALESCE(SUM(qty),0) q, COALESCE(SUM(received_qty),0) r FROM purchase_items WHERE po_id=?",
        (oid,)).fetchone()
    status = "COMPLETED" if row["r"] >= row["q"] and row["q"] > 0 else (
        "PARTIAL" if row["r"] > 0 else "CONFIRMED")
    cur = conn.execute("SELECT status FROM purchase_orders WHERE id=?", (oid,)).fetchone()
    if cur and cur["status"] in ("DRAFT", "CANCELLED"):
        return
    conn.execute("UPDATE purchase_orders SET status=? WHERE id=?", (status, oid))
    return status


def _validate_items(items) -> str:
    """明细行校验（v1.5.1）：数量必须 > 0，单价不能为负"""
    for idx, it in enumerate(items, start=1):
        qty = safe_float(it.get("qty"))
        price = safe_float(it.get("unit_price"))
        if not it.get("product_id"):
            return f"第 {idx} 行未选择商品"
        if qty <= 0:
            return f"第 {idx} 行数量无效：数量必须大于 0（当前 {it.get('qty')}）"
        if price < 0:
            return f"第 {idx} 行单价无效：单价不能为负（当前 {it.get('unit_price')}）"
    return ""


def _product_name(conn, pid) -> str:
    r = conn.execute("SELECT name FROM products WHERE id=?", (pid,)).fetchone()
    return r["name"] if r else f"商品#{pid}"


def save_order(header: dict, items: list, user: dict = None):
    """新建或更新采购订单。items: [{id?, product_id, qty, unit_price}]

    v1.5.1 修复（P0-1，与销售侧同构）：改为按明细 ID 增量更新，
    保留 received_qty，已收货明细不可删除/换商品/改低于已收量；
    状态只在草稿时由界面指定，其余按执行量重算。校验失败整单回滚。
    """
    if not header.get("supplier_id"):
        return False, "请选择供应商"
    if not items:
        return False, "请至少添加一条明细"
    err = _validate_items(items)
    if err:
        return False, err
    for key, label in (("order_date", "订单日期"), ("expected_date", "预计到货日期")):
        e = check_date(header.get(key), label, required=False)
        if e:
            return False, e

    try:
        with db.txn() as conn:
            oid = header.get("id")
            if oid:
                cur = conn.execute("SELECT status FROM purchase_orders WHERE id=?", (oid,)).fetchone()
                if not cur:
                    raise db.OpAbort("订单不存在")
                old_status = cur["status"]
                if old_status in ("COMPLETED", "CANCELLED"):
                    raise db.OpAbort("已完成/已取消的订单不可修改")
                conn.execute(
                    "UPDATE purchase_orders SET supplier_id=?, warehouse_id=?, order_date=?,"
                    " expected_date=?, remark=? WHERE id=?",
                    (header["supplier_id"], header["warehouse_id"], header.get("order_date") or today(),
                     header.get("expected_date") or "", header.get("remark", ""), oid))
                old_items = {r["id"]: dict(r) for r in
                             conn.execute("SELECT * FROM purchase_items WHERE po_id=?", (oid,)).fetchall()}
                action, ref = "修改采购订单", conn.execute(
                    "SELECT po_no FROM purchase_orders WHERE id=?", (oid,)).fetchone()["po_no"]
            else:
                old_status, old_items = None, {}
                po_no = db.next_no(conn, "PO", "PO")
                cur = conn.execute(
                    "INSERT INTO purchase_orders(po_no, supplier_id, warehouse_id, order_date, expected_date,"
                    " status, remark, created_by, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (po_no, header["supplier_id"], header["warehouse_id"], header.get("order_date") or today(),
                     header.get("expected_date") or "", header.get("status", "DRAFT"),
                     header.get("remark", ""), (user or {}).get("username", ""), now()))
                oid = cur.lastrowid
                action, ref = "新建采购订单", po_no

            keep = set()
            for idx, it in enumerate(items):
                qty = safe_float(it.get("qty"))
                price = safe_float(it.get("unit_price"))
                amount = round(qty * price, 2)
                iid = safe_int(it.get("id"))
                old = old_items.get(iid) if iid else None
                if old:
                    done = safe_float(old["received_qty"])
                    if done > 0:
                        if int(old["product_id"]) != int(it["product_id"]):
                            raise db.OpAbort(f"明细「{_product_name(conn, old['product_id'])}」"
                                             f"已收货 {done:g}，不能更换商品")
                        if qty + 1e-9 < done:
                            raise db.OpAbort(f"明细「{_product_name(conn, old['product_id'])}」"
                                             f"数量 {qty:g} 不能小于已收货数量 {done:g}")
                    conn.execute(
                        "UPDATE purchase_items SET product_id=?, qty=?, unit_price=?, amount=?, sort_no=?"
                        " WHERE id=?",
                        (it["product_id"], qty, price, amount, idx, iid))
                    keep.add(iid)
                else:
                    c = conn.execute(
                        "INSERT INTO purchase_items(po_id, product_id, qty, unit_price, amount,"
                        " received_qty, sort_no) VALUES(?,?,?,?,?,0,?)",
                        (oid, it["product_id"], qty, price, amount, idx))
                    keep.add(c.lastrowid)

            for iid, old in old_items.items():
                if iid in keep:
                    continue
                if safe_float(old["received_qty"]) > 0:
                    raise db.OpAbort(
                        f"已收货的明细不能删除（{_product_name(conn, old['product_id'])}"
                        f" 已收货 {safe_float(old['received_qty']):g}）。如需调整请先取消整单。")
                conn.execute("DELETE FROM purchase_items WHERE id=?", (iid,))

            _recalc(conn, oid)
            if old_status is None or old_status == "DRAFT":
                conn.execute("UPDATE purchase_orders SET status=? WHERE id=?",
                             (header.get("status") or "DRAFT", oid))
            else:
                _sync_status(conn, oid)
            from .auth import log
            log(conn, (user or {}).get("id"), (user or {}).get("username", ""),
                action, "采购", ref, f"共 {len(items)} 条明细")
            return True, ref
    except db.OpAbort as e:
        return False, str(e)


def confirm_order(oid: int, user: dict = None, force: bool = False):
    """确认采购订单。

    force：采购侧无信用额度管控，该参数仅为与 sales.confirm_order 保持
    接口对称而保留（v1.6.0 修复：库存管理「采购入库」对话框以
    force=True 调用，漏掉该参数会直接 TypeError，单据卡在草稿）。
    """
    with db.txn() as conn:
        row = conn.execute("SELECT * FROM purchase_orders WHERE id=?", (oid,)).fetchone()
        if not row:
            return False, "订单不存在"
        if row["status"] != "DRAFT":
            return False, "仅草稿可确认"
        conn.execute("UPDATE purchase_orders SET status='CONFIRMED' WHERE id=?", (oid,))
        from .auth import log
        log(conn, (user or {}).get("id"), (user or {}).get("username", ""), "确认采购订单", "采购", row["po_no"])
        return True, "已确认"


def cancel_order(oid: int, user: dict = None):
    with db.txn() as conn:
        row = conn.execute("SELECT * FROM purchase_orders WHERE id=?", (oid,)).fetchone()
        if not row:
            return False, "订单不存在"
        if row["status"] in ("COMPLETED", "CANCELLED"):
            return False, "当前状态不可取消"
        conn.execute("UPDATE purchase_orders SET status='CANCELLED' WHERE id=?", (oid,))
        from .auth import log
        log(conn, (user or {}).get("id"), (user or {}).get("username", ""), "取消采购订单", "采购", row["po_no"])
        return True, "已取消"


def receive_order(oid: int, receipts: dict, user: dict = None, move_date: str = None,
                  zone_map: dict = None):
    """收货入库。receipts: {item_id: 本次收货数量}
    zone_map: {item_id: zone_id} 可选，指定该明细入库落到的库区；
              省略则落到仓库默认存货区（v1.6.0 仓库分区可视化）。

    事务保证：多明细单据中任意一条校验不通过，本次**全部**明细的入库一并回滚，
    杜绝「界面提示失败、库存却已被加上」的半截写入。
    """
    zone_map = zone_map or {}
    try:
        with db.txn() as conn:
            order = conn.execute("SELECT * FROM purchase_orders WHERE id=?", (oid,)).fetchone()
            if not order:
                raise db.OpAbort("订单不存在")
            if order["status"] == "CANCELLED":
                raise db.OpAbort("已取消订单不能收货")
            if order["status"] == "DRAFT":
                conn.execute("UPDATE purchase_orders SET status='CONFIRMED' WHERE id=?", (oid,))

            total = 0
            for iid, q in receipts.items():
                q = safe_float(q)
                if q <= 0:
                    continue
                item = conn.execute("SELECT * FROM purchase_items WHERE id=? AND po_id=?",
                                    (iid, oid)).fetchone()
                if not item:
                    continue
                remain = safe_float(item["qty"]) - safe_float(item["received_qty"])
                if q - remain > 1e-6:
                    raise db.OpAbort(f"收货数量超过待收数量（剩余 {remain:g}）")
                md = move_date or today()
                inv_svc.apply_move(conn, item["product_id"], order["warehouse_id"], 1, q,
                                   item["unit_price"], "IN", "PURCHASE", order["po_no"], oid,
                                   f"采购收货 {order['po_no']}", user, md,
                                   zone_id=zone_map.get(iid))
                # received_at：记录最后一次收货日期，采购报表按实际入库日归月
                conn.execute("UPDATE purchase_items SET received_qty=?, received_at=? WHERE id=?",
                             (safe_float(item["received_qty"]) + q, md, iid))
                # 同步商品最新采购价
                conn.execute("UPDATE products SET cost_price=? WHERE id=?",
                             (item["unit_price"], item["product_id"]))
                total += q

            _sync_status(conn, oid)
            st = conn.execute("SELECT status FROM purchase_orders WHERE id=?", (oid,)).fetchone()["status"]
            from .auth import log
            log(conn, (user or {}).get("id"), (user or {}).get("username", ""), "采购收货入库", "采购",
                order["po_no"], f"本次收货 {total:g}，订单状态 {st}")
            return True, f"收货成功，订单状态：{st}"
    except db.OpAbort as e:
        return False, str(e)


def delete_order(oid: int, user: dict = None):
    with db.txn() as conn:
        row = conn.execute("SELECT * FROM purchase_orders WHERE id=?", (oid,)).fetchone()
        if not row:
            return False, "订单不存在"
        if row["status"] not in ("DRAFT", "CANCELLED"):
            return False, "仅草稿或已取消订单可删除"
        conn.execute("DELETE FROM purchase_items WHERE po_id=?", (oid,))
        conn.execute("DELETE FROM purchase_orders WHERE id=?", (oid,))
        from .auth import log
        log(conn, (user or {}).get("id"), (user or {}).get("username", ""), "删除采购订单", "采购", row["po_no"])
        return True, "已删除"


def pending_count():
    with db.txn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) c FROM purchase_orders WHERE status IN ('DRAFT','CONFIRMED','PARTIAL')"
        ).fetchone()
        return row["c"]
