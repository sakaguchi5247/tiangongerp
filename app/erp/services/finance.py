# -*- coding: utf-8 -*-
"""资金服务：应收 / 应付 / 收付款登记 / 账龄分析"""
from .. import db
from ..utils import now, today, safe_float, safe_int, days_between, check_date

PAY_SQL = """
SELECT p.*, pt.name AS partner_name, pt.code AS partner_code
FROM payments p JOIN partners pt ON pt.id=p.partner_id
"""

AGER_BUCKETS = [("未到期", -10 ** 9, -1), ("1-30天", 0, 30), ("31-60天", 31, 60),
                ("61-90天", 61, 90), ("90天以上", 91, 10 ** 9)]


def list_payments(keyword="", partner_id=None, direction="", date_from="", date_to="", limit=500):
    where, args = [], []
    if keyword:
        where.append("(p.pay_no LIKE ? OR pt.name LIKE ? OR p.remark LIKE ?)")
        args += [f"%{keyword}%"] * 3
    if partner_id:
        where.append("p.partner_id=?")
        args.append(partner_id)
    if direction:
        where.append("p.direction=?")
        args.append(direction)
    if date_from:
        where.append("p.pay_date >= ?")
        args.append(date_from)
    if date_to:
        where.append("p.pay_date <= ?")
        args.append(date_to)
    sql = PAY_SQL
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY p.id DESC LIMIT ?"
    args.append(limit)
    with db.txn() as conn:
        return db.rows_to_dicts(conn.execute(sql, args).fetchall())


def add_payment(data: dict, user: dict = None):
    direction = data.get("direction", "RECEIVE")
    amount = safe_float(data.get("amount"))
    if amount <= 0:
        return False, "金额必须大于 0"
    if not data.get("partner_id"):
        return False, "请选择往来单位"
    err = check_date(data.get("pay_date"), "收付款日期", required=False)
    if err:
        return False, err
    with db.txn() as conn:
        seq = "RECV" if direction == "RECEIVE" else "PAY"
        prefix = "SK" if direction == "RECEIVE" else "FK"
        pay_no = db.next_no(conn, seq, prefix)
        conn.execute(
            "INSERT INTO payments(pay_no, direction, partner_id, amount, method, pay_date,"
            " ref_type, ref_id, remark, created_by, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (pay_no, direction, data["partner_id"], amount, data.get("method", "银行转账"),
             data.get("pay_date") or today(), data.get("ref_type", ""), data.get("ref_id"),
             data.get("remark", ""), (user or {}).get("username", ""), now()))
        # 关联到单据时同步单据已收/已付金额
        ref_id = data.get("ref_id")
        if ref_id and data.get("ref_type") == "SALE":
            conn.execute("UPDATE sale_orders SET paid_amount=COALESCE(paid_amount,0)+? WHERE id=?",
                         (amount, ref_id))
        elif ref_id and data.get("ref_type") == "PURCHASE":
            conn.execute("UPDATE purchase_orders SET paid_amount=COALESCE(paid_amount,0)+? WHERE id=?",
                         (amount, ref_id))
        from .auth import log
        log(conn, (user or {}).get("id"), (user or {}).get("username", ""),
            "登记收款" if direction == "RECEIVE" else "登记付款", "资金", pay_no, f"金额 {amount:,.2f}")
        return True, pay_no


def delete_payment(pid: int, user: dict = None):
    with db.txn() as conn:
        row = conn.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
        if not row:
            return False, "记录不存在"
        if row["ref_id"] and row["ref_type"] == "SALE":
            conn.execute("UPDATE sale_orders SET paid_amount=MAX(0,COALESCE(paid_amount,0)-?) WHERE id=?",
                         (row["amount"], row["ref_id"]))
        elif row["ref_id"] and row["ref_type"] == "PURCHASE":
            conn.execute("UPDATE purchase_orders SET paid_amount=MAX(0,COALESCE(paid_amount,0)-?) WHERE id=?",
                         (row["amount"], row["ref_id"]))
        conn.execute("DELETE FROM payments WHERE id=?", (pid,))
        from .auth import log
        log(conn, (user or {}).get("id"), (user or {}).get("username", ""), "删除收付款", "资金", row["pay_no"])
        return True, "已删除"


# ---------------------------------------------------------------- 统一取数口径
# v1.5.1（P1-3）：过去 partner_balance/totals 按 payments 汇总（含无单据的
# 其他收款），aging 却按 sale_orders.paid_amount 逐单相减，同一笔账三个数字。
# 现在三处一律复用 _allocate()：
#   应收 = Σ(单据已执行金额 − 单据已核销) − 该单位未核销的收款（预收）
# 未核销收款按单据到期日从早到晚 FIFO 分摊，余额与账龄因此天然相等。
_AR_DOC_SQL = """
SELECT o.id, o.so_no AS no, o.order_date, COALESCE(o.paid_amount,0) AS paid,
       o.customer_id AS partner_id, pt.name AS partner_name,
       pt.payment_terms_days AS terms,
       COALESCE(SUM(i.delivered_qty*i.unit_price),0) AS amount
FROM sale_orders o JOIN sale_items i ON i.so_id=o.id
JOIN partners pt ON pt.id=o.customer_id
WHERE o.status<>'CANCELLED'
GROUP BY o.id
"""

_AP_DOC_SQL = """
SELECT o.id, o.po_no AS no, o.order_date, COALESCE(o.paid_amount,0) AS paid,
       o.supplier_id AS partner_id, pt.name AS partner_name,
       pt.payment_terms_days AS terms,
       COALESCE(SUM(i.received_qty*i.unit_price),0) AS amount
FROM purchase_orders o JOIN purchase_items i ON i.po_id=o.id
JOIN partners pt ON pt.id=o.supplier_id
WHERE o.status<>'CANCELLED'
GROUP BY o.id
"""


def _allocate(conn, kind: str) -> list:
    """按往来单位核销后返回单据级余额明细（唯一取数入口）"""
    direction = "RECEIVE" if kind == "AR" else "PAY"
    docs = [dict(r) for r in conn.execute(_AR_DOC_SQL if kind == "AR" else _AP_DOC_SQL)]
    paymap = {r["partner_id"]: float(r["v"]) for r in conn.execute(
        "SELECT partner_id, COALESCE(SUM(amount),0) v FROM payments WHERE direction=?"
        " GROUP BY partner_id", (direction,))}

    grouped = {}
    for d in docs:
        grouped.setdefault(d["partner_id"], []).append(d)

    for pid, ds in grouped.items():
        # 未核销收款 = 收款合计 − 已挂到单据上的部分（预收款、其他收款都在这里）
        credit = paymap.get(pid, 0.0) - sum(float(d["paid"]) for d in ds)
        for d in sorted(ds, key=lambda x: (_add_days(x["order_date"], safe_int(x["terms"], 30))
                                           or "9999-12-31")):
            bal = round(float(d["amount"]) - float(d["paid"]), 2)
            if credit > 0 and bal > 0:
                use = min(bal, credit)
                bal, credit = round(bal - use, 2), round(credit - use, 2)
            d["balance"] = bal

    # 只有收付款、没有任何单据的单位：整笔视为预收/预付（余额为负）
    for pid, total in paymap.items():
        if pid in grouped:
            continue
        if pid is None:
            continue
        row = conn.execute("SELECT name FROM partners WHERE id=?", (pid,)).fetchone()
        docs.append({
            "id": None, "no": "（预收/预付未核销）", "order_date": today(),
            "paid": 0, "partner_id": pid, "partner_name": row["name"] if row else "",
            "terms": 0, "amount": 0.0, "balance": round(-float(total), 2),
        })
    return docs


def partner_balance(partner_id: int, kind: str = "AR") -> float:
    """往来余额：应收 = 已出库金额 − 已收款（含预收/其他收款核销）"""
    with db.txn() as conn:
        docs = _allocate(conn, kind)
    return round(sum(float(d["balance"]) for d in docs if d["partner_id"] == partner_id), 2)


def totals() -> dict:
    with db.txn() as conn:
        ar_docs = _allocate(conn, "AR")
        ap_docs = _allocate(conn, "AP")
        ar_paid = conn.execute(
            "SELECT COALESCE(SUM(amount),0) v FROM payments WHERE direction='RECEIVE'").fetchone()["v"]
        ap_paid = conn.execute(
            "SELECT COALESCE(SUM(amount),0) v FROM payments WHERE direction='PAY'").fetchone()["v"]
        ar_inv = sum(float(d["amount"]) for d in ar_docs)
        ap_inv = sum(float(d["amount"]) for d in ap_docs)
        ar_bal = sum(float(d["balance"]) for d in ar_docs)
        ap_bal = sum(float(d["balance"]) for d in ap_docs)
    return {
        "ar_amount": round(ar_inv, 2),
        "ar_paid": round(float(ar_paid), 2),
        "ar_balance": round(ar_bal, 2),
        "ap_amount": round(ap_inv, 2),
        "ap_paid": round(float(ap_paid), 2),
        "ap_balance": round(ap_bal, 2),
        "cash_in": round(float(ar_paid), 2),
        "cash_out": round(float(ap_paid), 2),
        "net_cash": round(float(ar_paid) - float(ap_paid), 2),
    }


def aging(kind: str = "AR") -> dict:
    """按单据计算账龄分布（v1.5.1：与 partner_balance / totals 同一取数口径）"""
    with db.txn() as conn:
        rows = _allocate(conn, kind)

    buckets = {name: 0.0 for name, _, _ in AGER_BUCKETS}
    detail = []
    for r in rows:
        bal = round(float(r["balance"]), 2)
        if abs(bal) < 0.01:
            continue
        due = _add_days(r["order_date"], safe_int(r["terms"], 30))
        overdue = days_between(due, today()) if due else 0
        for name, lo, hi in AGER_BUCKETS:
            if lo <= overdue <= hi:
                buckets[name] += bal
                break
        detail.append({
            "no": r["no"], "partner": r["partner_name"], "partner_id": r["partner_id"],
            "order_date": r["order_date"], "due_date": due, "amount": round(float(r["amount"]), 2),
            "paid": round(float(r["paid"] or 0), 2), "balance": bal, "overdue_days": overdue,
        })
    detail.sort(key=lambda x: -x["overdue_days"])
    return {"buckets": buckets, "detail": detail,
            "total": round(sum(buckets.values()), 2)}


def _add_days(date_str: str, days: int) -> str:
    import datetime
    try:
        d = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
        return (d + datetime.timedelta(days=int(days))).strftime("%Y-%m-%d")
    except Exception:
        return ""


def partner_balances(kind: str = "AR", limit: int = 200) -> list:
    """按往来单位汇总余额"""
    with db.txn() as conn:
        ptype = "CUSTOMER" if kind == "AR" else "SUPPLIER"
        partners = conn.execute(
            "SELECT id, code, name, payment_terms_days, credit_limit FROM partners"
            " WHERE (type=? OR type='BOTH') AND active=1 ORDER BY code", (ptype,)).fetchall()
        out = []
        for p in partners:
            bal = partner_balance(p["id"], kind)
            if abs(bal) < 0.01:
                continue
            out.append({"id": p["id"], "code": p["code"], "name": p["name"],
                        "terms": p["payment_terms_days"], "credit_limit": p["credit_limit"],
                        "balance": bal,
                        "over_limit": bool(p["credit_limit"] and bal > float(p["credit_limit"]))})
        out.sort(key=lambda x: -x["balance"])
        return out[:limit]
