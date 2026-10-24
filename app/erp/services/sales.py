# -*- coding: utf-8 -*-
"""销售服务：销售订单 CRUD + 确认 + 出库发货 + 状态机 + 信用控制"""
from .. import db
from ..utils import now, today, safe_float, safe_int, check_date
from . import inventory as inv_svc
from . import finance as fin_svc

ORDER_SQL = """
SELECT o.*, pt.name AS partner_name, pt.code AS partner_code, w.name AS warehouse_name,
       pt.credit_limit, pt.payment_terms_days,
       (SELECT COALESCE(SUM(qty),0) FROM sale_items WHERE so_id=o.id) AS total_qty,
       (SELECT COALESCE(SUM(delivered_qty),0) FROM sale_items WHERE so_id=o.id) AS send_qty
FROM sale_orders o
JOIN partners pt ON pt.id=o.customer_id
JOIN warehouses w ON w.id=o.warehouse_id
"""

ITEM_SQL = """
SELECT i.*, p.sku, p.name AS product_name, p.unit, p.spec,
       COALESCE((SELECT qty FROM inventory v WHERE v.product_id=i.product_id AND v.warehouse_id=
                 (SELECT warehouse_id FROM sale_orders WHERE id=i.so_id)),0) AS avail_qty
FROM sale_items i JOIN products p ON p.id=i.product_id
"""


def list_orders(keyword="", customer_id=None, status="", date_from="", date_to="", limit=500):
    where, args = [], []
    if keyword:
        where.append("(o.so_no LIKE ? OR pt.name LIKE ? OR o.remark LIKE ?)")
        args += [f"%{keyword}%"] * 3
    if customer_id:
        where.append("o.customer_id=?")
        args.append(customer_id)
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
            conn.execute(ITEM_SQL + " WHERE i.so_id=? ORDER BY i.sort_no, i.id", (oid,)).fetchall()
        )
        return order


def _recalc(conn, oid: int):
    row = conn.execute("SELECT COALESCE(SUM(amount),0) t FROM sale_items WHERE so_id=?", (oid,)).fetchone()
    conn.execute("UPDATE sale_orders SET total_amount=? WHERE id=?", (row["t"], oid))


def _sync_status(conn, oid: int):
    row = conn.execute(
        "SELECT COALESCE(SUM(qty),0) q, COALESCE(SUM(delivered_qty),0) d FROM sale_items WHERE so_id=?",
        (oid,)).fetchone()
    status = "COMPLETED" if row["d"] >= row["q"] and row["q"] > 0 else (
        "PARTIAL" if row["d"] > 0 else "CONFIRMED")
    cur = conn.execute("SELECT status FROM sale_orders WHERE id=?", (oid,)).fetchone()
    if cur and cur["status"] in ("DRAFT", "CANCELLED"):
        return
    conn.execute("UPDATE sale_orders SET status=? WHERE id=?", (status, oid))
    return status


def _validate_items(items) -> str:
    """明细行校验（v1.5.1）：杜绝 0/负数量与负单价落库。

    过去 save_order 不做任何校验，实测可生成 total_amount=-200 的销售单。
    """
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


def _validate_dates(header, fields) -> str:
    """fields: [(字段名, 中文名)]，空值跳过（订单日期为空时由调用方取今天）"""
    for key, label in fields:
        err = check_date(header.get(key), label, required=False)
        if err:
            return err
    return ""


def save_order(header: dict, items: list, user: dict = None):
    """新建或更新销售订单。items: [{id?, product_id, qty, unit_price}]

    v1.5.1 修复（P0-1）：
      过去保存时先 `DELETE FROM sale_items WHERE so_id=?` 再整批重建，明细行的
      delivered_qty / unit_cost 被清零、状态被打回 DRAFT，而库存早已真实扣减，
      结果同一订单可以重复出库、毛利虚高。现改为按明细 ID 增量更新：
        · 已执行量（delivered_qty）与出库成本（unit_cost）原样保留；
        · 数量不得小于已出库量，已出库明细不得删除、不得换商品；
        · 状态只在草稿时由界面指定，其余一律按执行量重算。
      事务用 OpAbort 中止，校验失败时连表头一起回滚，不留半截修改。
    """
    if not header.get("customer_id"):
        return False, "请选择客户"
    if not items:
        return False, "请至少添加一条明细"
    err = _validate_items(items)
    if err:
        return False, err
    err = _validate_dates(header, (("order_date", "订单日期"), ("deliver_date", "交货日期")))
    if err:
        return False, err

    try:
        with db.txn() as conn:
            oid = header.get("id")
            if oid:
                cur = conn.execute("SELECT status FROM sale_orders WHERE id=?", (oid,)).fetchone()
                if not cur:
                    raise db.OpAbort("订单不存在")
                old_status = cur["status"]
                if old_status in ("COMPLETED", "CANCELLED"):
                    raise db.OpAbort("已完成/已取消的订单不可修改")
                conn.execute(
                    "UPDATE sale_orders SET customer_id=?, warehouse_id=?, order_date=?,"
                    " deliver_date=?, remark=?, channel=?, platform_order_no=?, currency=?,"
                    " rate=?, foreign_amount=? WHERE id=?",
                    (header["customer_id"], header["warehouse_id"], header.get("order_date") or today(),
                     header.get("deliver_date") or "", header.get("remark", ""),
                     header.get("channel", ""), header.get("platform_order_no", ""),
                     header.get("currency", "CNY"), header.get("rate", 1),
                     header.get("foreign_amount", 0), oid))
                old_items = {r["id"]: dict(r) for r in
                             conn.execute("SELECT * FROM sale_items WHERE so_id=?", (oid,)).fetchall()}
                action = "修改销售订单"
                ref = conn.execute("SELECT so_no FROM sale_orders WHERE id=?", (oid,)).fetchone()["so_no"]
            else:
                old_status, old_items = None, {}
                so_no = db.next_no(conn, "SO", "SO")
                cur = conn.execute(
                    "INSERT INTO sale_orders(so_no, customer_id, warehouse_id, order_date, deliver_date,"
                    " status, remark, channel, platform_order_no, currency, rate, foreign_amount,"
                    " created_by, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (so_no, header["customer_id"], header["warehouse_id"], header.get("order_date") or today(),
                     header.get("deliver_date") or "", header.get("status", "DRAFT"),
                     header.get("remark", ""), header.get("channel", ""), header.get("platform_order_no", ""),
                     header.get("currency", "CNY"), header.get("rate", 1), header.get("foreign_amount", 0),
                     (user or {}).get("username", ""), now()))
                oid = cur.lastrowid
                action, ref = "新建销售订单", so_no

            keep = set()
            for idx, it in enumerate(items):
                qty = safe_float(it.get("qty"))
                price = safe_float(it.get("unit_price"))
                amount = round(qty * price, 2)
                iid = safe_int(it.get("id"))
                old = old_items.get(iid) if iid else None
                if old:
                    done = safe_float(old["delivered_qty"])
                    if done > 0:
                        if int(old["product_id"]) != int(it["product_id"]):
                            raise db.OpAbort(f"明细「{_product_name(conn, old['product_id'])}」"
                                             f"已出库 {done:g}，不能更换商品")
                        if qty + 1e-9 < done:
                            raise db.OpAbort(f"明细「{_product_name(conn, old['product_id'])}」"
                                             f"数量 {qty:g} 不能小于已出库数量 {done:g}")
                    conn.execute(
                        "UPDATE sale_items SET product_id=?, qty=?, unit_price=?, amount=?, sort_no=?"
                        " WHERE id=?",
                        (it["product_id"], qty, price, amount, idx, iid))
                    keep.add(iid)
                else:
                    c = conn.execute(
                        "INSERT INTO sale_items(so_id, product_id, qty, unit_price, unit_cost, amount,"
                        " delivered_qty, sort_no) VALUES(?,?,?,?,0,?,0,?)",
                        (oid, it["product_id"], qty, price, amount, idx))
                    keep.add(c.lastrowid)

            for iid, old in old_items.items():
                if iid in keep:
                    continue
                if safe_float(old["delivered_qty"]) > 0:
                    raise db.OpAbort(
                        f"已出库的明细不能删除（{_product_name(conn, old['product_id'])}"
                        f" 已出库 {safe_float(old['delivered_qty']):g}）。如需调整请先取消整单。")
                conn.execute("DELETE FROM sale_items WHERE id=?", (iid,))

            _recalc(conn, oid)
            if old_status is None or old_status == "DRAFT":
                conn.execute("UPDATE sale_orders SET status=? WHERE id=?",
                             (header.get("status") or "DRAFT", oid))
            else:
                _sync_status(conn, oid)      # 已确认/部分出库：按执行量重算，禁止被打回草稿

            from .auth import log
            log(conn, (user or {}).get("id"), (user or {}).get("username", ""),
                action, "销售", ref, f"共 {len(items)} 条明细")
            return True, ref
    except db.OpAbort as e:
        return False, str(e)


def confirm_order(oid: int, user: dict = None, force: bool = False):
    """确认订单：校验库存可用量与客户信用额度"""
    order = get_order(oid)
    if not order:
        return False, "订单不存在"
    if order["status"] != "DRAFT":
        return False, "仅草稿可确认"

    with db.txn() as conn:
        for it in order["items"]:
            stock = inv_svc.current_stock(conn, it["product_id"], order["warehouse_id"])
            if float(stock["qty"]) + 1e-9 < float(it["qty"]):
                return False, f"{it['product_name']} 库存不足（可用 {stock['qty']:g}，需求 {it['qty']:g}）"

    if not force:
        balance = fin_svc.partner_balance(order["customer_id"], "AR")
        limit = safe_float(order.get("credit_limit"))
        if limit > 0 and balance + safe_float(order["total_amount"]) > limit:
            return False, (f"超出信用额度：额度 {limit:,.2f}，已有应收 {balance:,.2f}，"
                           f"本单 {safe_float(order['total_amount']):,.2f}。可勾选“强制确认”。")

    with db.txn() as conn:
        conn.execute("UPDATE sale_orders SET status='CONFIRMED' WHERE id=?", (oid,))
        from .auth import log
        log(conn, (user or {}).get("id"), (user or {}).get("username", ""),
            "确认销售订单", "销售", order["so_no"])
        return True, "已确认，可开始出库发货"


def cancel_order(oid: int, user: dict = None):
    with db.txn() as conn:
        row = conn.execute("SELECT * FROM sale_orders WHERE id=?", (oid,)).fetchone()
        if not row:
            return False, "订单不存在"
        if row["status"] in ("COMPLETED", "CANCELLED"):
            return False, "当前状态不可取消"
        conn.execute("UPDATE sale_orders SET status='CANCELLED' WHERE id=?", (oid,))
        from .auth import log
        log(conn, (user or {}).get("id"), (user or {}).get("username", ""), "取消销售订单", "销售", row["so_no"])
        return True, "已取消"


def deliver_order(oid: int, deliveries: dict, user: dict = None, move_date: str = None,
                  zone_map: dict = None):
    """出库发货。deliveries: {item_id: 本次出库数量}
    zone_map: {item_id: zone_id} 可选，指定该明细出库从哪个库区扣减；
              省略则从仓库默认存货区扣减（v1.6.0 仓库分区可视化）。

    事务保证：多明细单据中任意一条校验不通过，本次**全部**明细的出库一并回滚，
    杜绝「界面提示失败、库存却已被扣掉」的半截写入。
    """
    zone_map = zone_map or {}
    try:
        with db.txn() as conn:
            order = conn.execute("SELECT * FROM sale_orders WHERE id=?", (oid,)).fetchone()
            if not order:
                raise db.OpAbort("订单不存在")
            if order["status"] == "CANCELLED":
                raise db.OpAbort("已取消订单不能出库")
            if order["status"] == "DRAFT":
                conn.execute("UPDATE sale_orders SET status='CONFIRMED' WHERE id=?", (oid,))

            total = 0
            for iid, q in deliveries.items():
                q = safe_float(q)
                if q <= 0:
                    continue
                item = conn.execute("SELECT * FROM sale_items WHERE id=? AND so_id=?", (iid, oid)).fetchone()
                if not item:
                    continue
                remain = safe_float(item["qty"]) - safe_float(item["delivered_qty"])
                if q - remain > 1e-6:
                    raise db.OpAbort(f"出库数量超过订单未发数量（剩余可发 {remain:g}）")
                stock = inv_svc.current_stock(conn, item["product_id"], order["warehouse_id"])
                if float(stock["qty"]) + 1e-9 < q:
                    raise db.OpAbort(
                        f"库存不足：{_product_name(conn, item['product_id'])} "
                        f"可用 {float(stock['qty']):g}，本次需求 {q:g}")
                md = move_date or today()
                cost = float(stock["avg_cost"]) or safe_float(item["unit_cost"])
                inv_svc.apply_move(conn, item["product_id"], order["warehouse_id"], -1, q, cost,
                                   "OUT", "SALE", order["so_no"], oid,
                                   f"销售出库 {order['so_no']}", user, md,
                                   zone_id=zone_map.get(iid))
                # delivered_at：记录最后一次出库日期，报表据此按出库日确认收入
                conn.execute("UPDATE sale_items SET delivered_qty=?, unit_cost=?, delivered_at=? WHERE id=?",
                             (safe_float(item["delivered_qty"]) + q, cost, md, iid))
                total += q

            _sync_status(conn, oid)
            st = conn.execute("SELECT status FROM sale_orders WHERE id=?", (oid,)).fetchone()["status"]
            from .auth import log
            log(conn, (user or {}).get("id"), (user or {}).get("username", ""), "销售出库发货", "销售",
                order["so_no"], f"本次出库 {total:g}，订单状态 {st}")
            return True, f"出库成功，订单状态：{st}"
    except db.OpAbort as e:
        return False, str(e)


def _product_name(conn, pid) -> str:
    r = conn.execute("SELECT name FROM products WHERE id=?", (pid,)).fetchone()
    return r["name"] if r else f"商品#{pid}"


def delete_order(oid: int, user: dict = None):
    with db.txn() as conn:
        row = conn.execute("SELECT * FROM sale_orders WHERE id=?", (oid,)).fetchone()
        if not row:
            return False, "订单不存在"
        if row["status"] not in ("DRAFT", "CANCELLED"):
            return False, "仅草稿或已取消订单可删除"
        conn.execute("DELETE FROM sale_items WHERE so_id=?", (oid,))
        conn.execute("DELETE FROM sale_orders WHERE id=?", (oid,))
        from .auth import log
        log(conn, (user or {}).get("id"), (user or {}).get("username", ""), "删除销售订单", "销售", row["so_no"])
        return True, "已删除"


def otif_rate(days: int = 90) -> dict:
    """准时足额交付率 OTIF（按订单统计近 N 天）

    v1.5.1 修复（P2-1）：旧判定式里 `or status == "COMPLETED"` 让所有已完成的订单
    一律算准时——应交日 10-04、10-14 才交完，OTIF 仍显示 100%。
    现改为拿真实出库日期（stock_moves 中该单最后一条 SALE 流水的 move_date）
    与应交日比较；未填应交日的按准时计。
    """
    with db.txn() as conn:
        rows = conn.execute(
            """SELECT o.id, o.deliver_date, o.status,
                      COALESCE(SUM(i.qty),0) q, COALESCE(SUM(i.delivered_qty),0) d,
                      (SELECT MAX(m.move_date) FROM stock_moves m
                        WHERE m.ref_type='SALE' AND m.ref_id=o.id) AS last_out
               FROM sale_orders o JOIN sale_items i ON i.so_id=o.id
               WHERE o.status<>'CANCELLED' AND o.order_date >= date('now',?)
               GROUP BY o.id""", (f"-{int(days)} day",)).fetchall()
    total = len(rows)
    if not total:
        return {"total": 0, "on_time": 0, "rate": 0.0}
    on_time = 0
    for r in rows:
        in_full = float(r["d"]) >= float(r["q"]) - 1e-9
        dd = r["deliver_date"] or ""
        # 实际交付日：取最后一次出库日；尚未出库（无流水）则按今天评估是否已逾期
        actual = (r["last_out"] or "") or today()
        on_time_due = (not dd) or actual <= dd
        if in_full and on_time_due:
            on_time += 1
    return {"total": total, "on_time": on_time, "rate": on_time / total}
