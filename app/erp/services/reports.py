# -*- coding: utf-8 -*-
"""报表与经营指标（KPI）计算

指标口径说明：
  库存周转率   = 期间销售成本 COGS ÷ 平均库存金额（v1.5.1 起按 (期初+期末)/2 计算，
                 期初用「期末 − 期间净入库金额」倒推，无需历史快照）
  库存周转天数 DIO = 365 ÷ 库存周转率
  应收周转天数 DSO = 应收余额 ÷ 日均销售额
  应付周转天数 DPO = 应付余额 ÷ 日均销售成本
  现金转换周期 CCC = DIO + DSO − DPO
  毛利率       = (销售收入 − 销售成本) ÷ 销售收入
  再订货点 ROP = 日均需求 × 采购提前期 + 安全库存
  经济订货量 EOQ = √(2 × 年需求量 × 每次订货成本 ÷ 单位年持有成本)
  缺货率       = 低于安全库存的 SKU 数 ÷ 有效 SKU 数
  呆滞库存     = 超过 N 天无出库且仍有库存的商品金额
"""
import math
from .. import db
from ..utils import today, safe_float, div, add_days

REVENUE_EXPR = "COALESCE(SUM(i.delivered_qty * i.unit_price),0)"
COGS_EXPR = "COALESCE(SUM(i.delivered_qty * i.unit_cost),0)"

# v1.5.1（P2-2）：收入在出库时确认，而不是下单时。
# 明细行记录了最后一次出库日 delivered_at；历史数据该列为空时退回订单日期，
# 保证老账套升级后口径不突变。
DELIVER_DATE = "COALESCE(NULLIF(i.delivered_at,''), o.order_date)"
RECV_DATE = "COALESCE(NULLIF(i.received_at,''), o.order_date)"


def _params():
    return db.get_params()


def monthly_trend(months: int = 12) -> list:
    """近 N 个月销售趋势（收入 / 成本 / 毛利，按出库月份归集）"""
    with db.txn() as conn:
        rows = conn.execute(
            f"""SELECT substr({DELIVER_DATE},1,7) AS ym,
                       {REVENUE_EXPR} AS revenue, {COGS_EXPR} AS cogs,
                       COALESCE(SUM(i.delivered_qty),0) AS qty,
                       COUNT(DISTINCT o.id) AS orders
                FROM sale_items i JOIN sale_orders o ON o.id=i.so_id
                WHERE o.status<>'CANCELLED' AND i.delivered_qty>0
                GROUP BY ym ORDER BY ym DESC LIMIT ?""", (months,)).fetchall()
    out = []
    for r in reversed(rows):
        rev, cogs = float(r["revenue"]), float(r["cogs"])
        out.append({"ym": r["ym"], "revenue": round(rev, 2), "cogs": round(cogs, 2),
                    "gross": round(rev - cogs, 2), "margin": div(rev - cogs, rev),
                    "qty": round(float(r["qty"]), 2), "orders": r["orders"]})
    return out


def top_products(limit: int = 10, days: int = 90) -> list:
    with db.txn() as conn:
        rows = conn.execute(
            f"""SELECT p.id, p.sku, p.name, p.unit,
                       COALESCE(SUM(i.delivered_qty),0) AS qty,
                       {REVENUE_EXPR} AS revenue, {COGS_EXPR} AS cogs
                FROM sale_items i JOIN sale_orders o ON o.id=i.so_id
                JOIN products p ON p.id=i.product_id
                WHERE o.status<>'CANCELLED' AND i.delivered_qty>0
                  AND {DELIVER_DATE} >= date('now',?)
                GROUP BY p.id ORDER BY revenue DESC LIMIT ?""",
            (f"-{int(days)} day", limit)).fetchall()
    out = []
    for r in rows:
        rev, cogs = float(r["revenue"]), float(r["cogs"])
        out.append({"sku": r["sku"], "name": r["name"], "unit": r["unit"],
                    "qty": round(float(r["qty"]), 2), "revenue": round(rev, 2),
                    "cogs": round(cogs, 2), "gross": round(rev - cogs, 2),
                    "margin": div(rev - cogs, rev)})
    return out


def sales_by_customer(days: int = 90) -> list:
    with db.txn() as conn:
        rows = conn.execute(
            f"""SELECT pt.code, pt.name, COUNT(DISTINCT o.id) AS orders,
                       COALESCE(SUM(i.delivered_qty),0) AS qty,
                       {REVENUE_EXPR} AS revenue, {COGS_EXPR} AS cogs
                FROM sale_items i JOIN sale_orders o ON o.id=i.so_id
                JOIN partners pt ON pt.id=o.customer_id
                WHERE o.status<>'CANCELLED' AND i.delivered_qty>0
                  AND {DELIVER_DATE} >= date('now',?)
                GROUP BY pt.id ORDER BY revenue DESC""", (f"-{int(days)} day",)).fetchall()
    out = []
    for r in rows:
        rev, cogs = float(r["revenue"]), float(r["cogs"])
        out.append({"code": r["code"], "name": r["name"], "orders": r["orders"],
                    "qty": round(float(r["qty"]), 2), "revenue": round(rev, 2),
                    "gross": round(rev - cogs, 2), "margin": div(rev - cogs, rev)})
    return out


def purchase_by_supplier(days: int = 90) -> list:
    with db.txn() as conn:
        rows = conn.execute(
            """SELECT pt.code, pt.name, COUNT(DISTINCT o.id) AS orders,
                      COALESCE(SUM(i.received_qty),0) AS qty,
                      COALESCE(SUM(i.received_qty*i.unit_price),0) AS amount
               FROM purchase_items i JOIN purchase_orders o ON o.id=i.po_id
               JOIN partners pt ON pt.id=o.supplier_id
               WHERE o.status<>'CANCELLED' AND i.received_qty>0
                 AND {RECV_DATE} >= date('now',?)
               GROUP BY pt.id ORDER BY amount DESC""", (f"-{int(days)} day",)).fetchall()
    return [{"code": r["code"], "name": r["name"], "orders": r["orders"],
             "qty": round(float(r["qty"]), 2), "amount": round(float(r["amount"]), 2)}
            for r in rows]


def sales_detail(date_from: str = "", date_to: str = ""):
    where, args = ["o.status<>'CANCELLED' AND i.delivered_qty>0"], []
    if date_from:
        where.append(f"{DELIVER_DATE}>=?"); args.append(date_from)
    if date_to:
        where.append(f"{DELIVER_DATE}<=?"); args.append(date_to)
    with db.txn() as conn:
        rows = conn.execute(
            f"""SELECT o.so_no, o.order_date, {DELIVER_DATE} AS deliver_date, o.status,
                       pt.name AS customer,
                       p.sku, p.name AS product, p.unit, i.qty AS order_qty,
                       i.delivered_qty, i.unit_price, i.unit_cost,
                       ROUND(i.delivered_qty*i.unit_price,2) AS amount,
                       ROUND(i.delivered_qty*(i.unit_price-i.unit_cost),2) AS gross
                FROM sale_items i JOIN sale_orders o ON o.id=i.so_id
                JOIN partners pt ON pt.id=o.customer_id
                JOIN products p ON p.id=i.product_id
                WHERE {' AND '.join(where)} ORDER BY o.order_date DESC, o.id DESC""", args).fetchall()
    return db.rows_to_dicts(rows)


def inventory_analysis() -> list:
    """库存结构分析：周转、缺货风险、呆滞、EOQ 建议"""
    p = _params()
    window = int(p.get("turnover_window_days", 90))
    dull_days = int(p.get("dull_days", 180))
    order_cost = float(p.get("order_cost", 200))
    holding_rate = float(p.get("holding_rate", 0.25))
    year_days = float(p.get("days_in_year", 365))

    with db.txn() as conn:
        rows = conn.execute(
            """SELECT p.id, p.sku, p.name, p.unit, p.cost_price, p.sale_price,
                      p.safety_stock, p.lead_time_days, p.reorder_point, p.abc_class,
                      p.created_at, c.name AS category_name,
                      COALESCE((SELECT SUM(v.qty) FROM inventory v WHERE v.product_id=p.id),0) AS qty,
                      COALESCE((SELECT SUM(v.qty*v.avg_cost) FROM inventory v WHERE v.product_id=p.id),0) AS value,
                      COALESCE((SELECT SUM(m.qty) FROM stock_moves m WHERE m.product_id=p.id
                                AND m.direction=-1 AND m.move_date>=date('now',?)),0) AS out_qty,
                      COALESCE((SELECT SUM(m.amount) FROM stock_moves m WHERE m.product_id=p.id
                                AND m.direction=-1 AND m.ref_type='SALE'
                                AND m.move_date>=date('now',?)),0) AS cogs,
                      COALESCE((SELECT SUM(m.direction*m.amount) FROM stock_moves m
                                WHERE m.product_id=p.id AND m.move_date>=date('now',?)),0) AS net_amount,
                      (SELECT MAX(m.move_date) FROM stock_moves m WHERE m.product_id=p.id AND m.direction=-1) AS last_out
               FROM products p LEFT JOIN categories c ON c.id=p.category_id
               WHERE p.active=1 ORDER BY p.sku""",
            (f"-{window} day", f"-{window} day", f"-{window} day")).fetchall()

    cutoff = add_days(today(), -dull_days)
    out = []
    for r in rows:
        qty = float(r["qty"]); value = float(r["value"])
        cogs = float(r["cogs"])
        # v1.5.1（P2-3）：分母改用「平均库存金额」。
        # 期初 = 期末 − 期间净入库金额（倒推），无需历史快照；倒推为负则按 0 计
        start_value = max(value - float(r["net_amount"]), 0.0)
        avg_value = (start_value + value) / 2
        turnover = div(cogs * (year_days / window), avg_value) if avg_value else 0.0
        dio = div(year_days, turnover) if turnover else 0.0
        daily_demand = div(float(r["out_qty"]), window)
        annual_demand = daily_demand * year_days
        holding_unit = float(r["cost_price"]) * holding_rate
        eoq = math.sqrt(2 * annual_demand * order_cost / holding_unit) if holding_unit > 0 else 0.0
        last_out = r["last_out"] or ""
        # v1.5.1（P2-4）：从未出库过的新品不再一律判呆滞 ——
        # 有出库记录按最后出库日判定；从未出库的按建档日在库天数判定。
        if qty <= 0:
            dull = False
        elif last_out:
            dull = last_out < cutoff
        else:
            created = (r["created_at"] or "")[:10]
            dull = bool(created and created < cutoff)
        out.append({
            "sku": r["sku"], "name": r["name"], "category": r["category_name"] or "",
            "unit": r["unit"], "qty": round(qty, 2), "value": round(value, 2),
            "avg_cost": div(value, qty), "turnover": round(turnover, 2), "dio": round(dio, 1),
            "daily_demand": round(daily_demand, 3), "safety_stock": float(r["safety_stock"]),
            "lead_time": r["lead_time_days"], "rop": float(r["reorder_point"]),
            "eoq": round(eoq, 1), "abc": r["abc_class"],
            "risk": "缺货" if qty <= float(r["safety_stock"]) else (
                "预警" if qty <= float(r["reorder_point"]) else "正常"),
            "dull": dull,
        })
    return out


def turnover_kpi(window: int = 90) -> dict:
    """v1.5.1（P2-3）：分母改为平均库存金额（期初倒推 + 期末）÷ 2"""
    p = _params()
    year_days = float(p.get("days_in_year", 365))
    with db.txn() as conn:
        cogs = conn.execute(
            """SELECT COALESCE(SUM(amount),0) v FROM stock_moves
               WHERE direction=-1 AND ref_type='SALE' AND move_date>=date('now',?)""",
            (f"-{int(window)} day",)).fetchone()["v"]
        inv = conn.execute("SELECT COALESCE(SUM(qty*avg_cost),0) v FROM inventory").fetchone()["v"]
        net = conn.execute(
            """SELECT COALESCE(SUM(direction*amount),0) v FROM stock_moves
               WHERE move_date>=date('now',?)""", (f"-{int(window)} day",)).fetchone()["v"]
    cogs, inv, net = float(cogs), float(inv), float(net)
    start = max(inv - net, 0.0)
    avg_inv = (start + inv) / 2
    turnover = div(cogs * (year_days / window), avg_inv)
    return {"cogs": round(cogs, 2), "inventory_value": round(inv, 2),
            "avg_inventory": round(avg_inv, 2),
            "turnover": round(turnover, 2), "dio": round(div(year_days, turnover), 1)}


def period_sales(days: int = 30) -> dict:
    with db.txn() as conn:
        row = conn.execute(
            f"""SELECT {REVENUE_EXPR} AS revenue, {COGS_EXPR} AS cogs,
                       COALESCE(SUM(i.delivered_qty),0) AS qty, COUNT(DISTINCT o.id) AS orders
                FROM sale_items i JOIN sale_orders o ON o.id=i.so_id
                WHERE o.status<>'CANCELLED' AND i.delivered_qty>0
                  AND {DELIVER_DATE}>=date('now',?)""", (f"-{int(days)} day",)).fetchone()
    rev, cogs = float(row["revenue"]), float(row["cogs"])
    return {"revenue": round(rev, 2), "cogs": round(cogs, 2), "gross": round(rev - cogs, 2),
            "margin": div(rev - cogs, rev), "qty": round(float(row["qty"]), 2), "orders": row["orders"]}


def stock_health() -> dict:
    """库存健康度：缺货率、呆滞金额、低于安全库存 SKU"""
    rows = inventory_analysis()
    total = len(rows)
    risky = [r for r in rows if r["risk"] == "缺货"]
    dull_value = sum(r["value"] for r in rows if r["dull"])
    dull_skus = len([r for r in rows if r["dull"]])
    return {"sku_total": total, "sku_shortage": len(risky),
            "stockout_rate": div(len(risky), total),
            "dull_skus": dull_skus, "dull_value": round(dull_value, 2),
            "shortage_list": sorted(risky, key=lambda x: x["qty"])[:8]}


def abc_classify(window: int = 180) -> int:
    """按销售额累计占比重算 ABC 分类（A:0-80%, B:80-95%, C:95-100%）

    v1.5.1（P2-5）：过去只更新有销售额的 SKU，无销售记录的商品保留旧分类，
    结果「重算」后仍有商品顶着过期的 A 类。现改为覆盖全部 SKU，
    无销售额的一律归入 C 类。
    """
    rows = top_products(limit=100000, days=window)
    with db.txn() as conn:
        all_skus = [r["sku"] for r in conn.execute("SELECT sku FROM products")]
    total = sum(r["revenue"] for r in rows)
    if total <= 0:
        mapping = {sku: "C" for sku in all_skus}
    else:
        cum = 0.0
        mapping = {}
        for r in rows:
            cum += r["revenue"]
            ratio = cum / total
            mapping[r["sku"]] = "A" if ratio <= 0.8 else ("B" if ratio <= 0.95 else "C")
    with db.txn() as conn:
        for sku in all_skus:
            conn.execute("UPDATE products SET abc_class=? WHERE sku=?", (mapping.get(sku, "C"), sku))
    return len(all_skus)


def dashboard() -> dict:
    """经营看板所需的全部 KPI"""
    from . import finance as fin_svc, inventory as inv_svc, purchase as po_svc, sales as so_svc
    p = _params()
    window = int(p.get("turnover_window_days", 90))
    year_days = float(p.get("days_in_year", 365))

    k30 = period_sales(30)
    k90 = period_sales(window)
    tk = turnover_kpi(window)
    fin = fin_svc.totals()
    health = stock_health()
    stock = inv_svc.stock_summary()

    daily_rev = div(k90["revenue"], window)
    daily_cogs = div(k90["cogs"], window)
    dso = div(fin["ar_balance"], daily_rev)
    dpo = div(fin["ap_balance"], daily_cogs)
    dio = tk["dio"] or div(year_days, tk["turnover"])
    ccc = dio + dso - dpo

    otif = so_svc.otif_rate(window)
    with db.txn() as conn:
        pending_po = conn.execute(
            "SELECT COUNT(*) c FROM purchase_orders WHERE status IN ('DRAFT','CONFIRMED','PARTIAL')").fetchone()["c"]
        pending_so = conn.execute(
            "SELECT COUNT(*) c FROM sale_orders WHERE status IN ('DRAFT','CONFIRMED','PARTIAL')").fetchone()["c"]
        in_transit = conn.execute(
            """SELECT COALESCE(SUM(i.qty-i.received_qty),0) v FROM purchase_items i
               JOIN purchase_orders o ON o.id=i.po_id WHERE o.status IN ('CONFIRMED','PARTIAL')""").fetchone()["v"]

    return {
        "sales_30": k30, "sales_90": k90,
        "stock_value": round(float(stock["value"]), 2), "stock_qty": round(float(stock["qty"]), 2),
        "sku_count": stock["skus"],
        "turnover": tk["turnover"], "dio": round(dio, 1), "cogs_90": tk["cogs"],
        "dso": round(dso, 1), "dpo": round(dpo, 1), "ccc": round(ccc, 1),
        "ar_balance": fin["ar_balance"], "ap_balance": fin["ap_balance"],
        "net_cash": fin["net_cash"], "cash_in": fin["cash_in"], "cash_out": fin["cash_out"],
        "stockout_rate": health["stockout_rate"], "sku_shortage": health["sku_shortage"],
        "dull_value": health["dull_value"], "dull_skus": health["dull_skus"],
        "otif": otif, "pending_po": pending_po, "pending_so": pending_so,
        "in_transit": round(float(in_transit), 2),
        "monthly": monthly_trend(6),
        "top_products": top_products(5, 90),
        "shortage": health["shortage_list"],
        "gross_margin_target": p.get("gross_margin_target", 0.25),
    }
