# -*- coding: utf-8 -*-
"""演示数据生成器：商品 / 往来单位 / 采购 / 销售 / 收付款 / 库存流水

生成的数据全部通过服务层写入，保证库存流水、成本、应收应付口径一致。
用法：python main.py --seed
"""
import calendar
import datetime
import random

from . import db
from .services import master, purchase, sales, finance, inventory, auth, reports, zones as zone_svc
from .utils import today

PRODUCT_SEED = [
    ("P1001", "冷轧钢板 1.0mm", "原材料", "张", 118.0, 168.0, "Q235, 1.0×1250×2500"),
    ("P1002", "冷轧钢板 2.0mm", "原材料", "张", 196.0, 272.0, "Q235, 2.0×1250×2500"),
    ("P1003", "铝合金型材 6063", "原材料", "米", 21.5, 32.0, "6063-T5 阳极氧化"),
    ("P1004", "不锈钢管 304", "原材料", "米", 38.0, 55.0, "Φ25×2.0"),
    ("P1005", "铜排 TMY", "原材料", "公斤", 62.0, 84.0, "30×4mm"),
    ("P2001", "控制柜壳体 A型", "成品", "台", 860.0, 1380.0, "600×800×2000"),
    ("P2002", "控制柜壳体 B型", "成品", "台", 1180.0, 1880.0, "800×1000×2200"),
    ("P2003", "配电箱 12回路", "成品", "台", 320.0, 520.0, "暗装 12 回路"),
    ("P2004", "配电箱 24回路", "成品", "台", 470.0, 760.0, "暗装 24 回路"),
    ("P2005", "变频控制柜 5.5kW", "成品", "台", 2650.0, 4280.0, "含变频器与散热"),
    ("P2006", "PLC 控制箱 标准型", "成品", "台", 1980.0, 3180.0, "西门子 S7-1200"),
    ("P3001", "断路器 C32A 2P", "包装耗材", "只", 28.0, 46.0, "正泰 2P"),
    ("P3002", "断路器 C63A 3P", "包装耗材", "只", 62.0, 98.0, "正泰 3P"),
    ("P3003", "接线端子 UK2.5", "包装耗材", "只", 1.6, 3.2, "导轨式"),
    ("P3004", "线缆 BV 2.5mm²", "包装耗材", "米", 2.4, 4.2, "100米/卷"),
    ("P3005", "线缆 YJV 4×16", "包装耗材", "米", 18.6, 29.8, "低压电力电缆"),
    ("P3006", "扎带 100mm", "包装耗材", "包", 6.5, 12.0, "100根/包"),
    ("P3007", "标签纸 热敏", "包装耗材", "卷", 9.0, 16.0, "50×30mm"),
    ("P4001", "A4 复印纸", "办公用品", "包", 18.5, 28.0, "70g 500张"),
    ("P4002", "中性笔 0.5mm", "办公用品", "支", 1.8, 3.5, "黑色"),
    ("P4003", "文件柜 钢制", "办公用品", "个", 420.0, 680.0, "1800×900×400"),
    ("P4004", "办公椅 网布", "办公用品", "把", 265.0, 420.0, "人体工学"),
]

SUPPLIERS = [
    ("S001", "华东钢材有限公司", "张建国", "021-6688-1001", "上海市宝山区钢铁路88号", 30, 800000),
    ("S002", "南方铝业集团", "李海燕", "020-3888-2202", "广州市黄埔区铝业大道12号", 45, 600000),
    ("S003", "精工五金制造厂", "王志强", "0574-8776-3303", "宁波市北仑区精工路5号", 30, 400000),
    ("S004", "恒信电缆股份", "陈美玲", "0512-6655-4404", "苏州市吴江区电缆园3号", 60, 900000),
    ("S005", "博远办公设备", "赵启明", "010-8222-5505", "北京市朝阳区办公街9号", 15, 200000),
]

CUSTOMERS = [
    ("C001", "中建安装工程有限公司", "刘宏伟", "025-8666-7001", "南京市建邺区江东中路100号", 45, 1500000),
    ("C002", "华能新能源设备公司", "孙丽华", "010-5777-8002", "北京市西城区金融街7号", 60, 2000000),
    ("C003", "智造自动化科技", "周文涛", "0755-8333-9003", "深圳市南山区科技园A座", 30, 800000),
    ("C004", "宏图电力工程", "吴建华", "027-8444-1004", "武汉市洪山区光谷大道66号", 45, 1200000),
    ("C005", "瑞泰成套电气", "郑晓东", "0531-6999-2005", "济南市历下区工业南路21号", 30, 600000),
    ("C006", "联创智能装备", "冯雅静", "0571-8888-3006", "杭州市滨江区物联网街8号", 30, 700000),
    ("C007", "远洋船舶配套", "何俊峰", "0411-8222-4007", "大连市中山区港湾街5号", 60, 900000),
    ("C008", "新纪元数据中心", "许一鸣", "023-6777-5008", "重庆市渝北区数据谷1号", 30, 1000000),
]

# 热销品（其余商品仅采购不出售，用于演示呆滞库存分析）
HOT_SKUS = {p[0] for p in PRODUCT_SEED[:16]}

DEMO_USERS = [
    ("manager", "张总经理", "manager"),
    ("sales", "李销售", "sales"),
    ("purchase", "王采购", "purchase"),
    ("warehouse", "赵仓管", "warehouse"),
    ("finance", "钱财务", "finance"),
]


def _month_starts(n: int):
    d = datetime.date.today().replace(day=1)
    out = []
    for i in range(n - 1, -1, -1):
        y, m = d.year, d.month - i
        while m <= 0:
            m += 12
            y -= 1
        out.append(datetime.date(y, m, 1))
    return out


def run(months: int = 6, verbose: bool = True) -> dict:
    random.seed(20261009)
    admin = {"id": 1, "username": "admin"}
    stat = {"products": 0, "partners": 0, "po": 0, "so": 0, "pay": 0}

    # ---------------- 基础数据 ----------------
    cats = {c["name"]: c["id"] for c in master.list_categories()}
    if not master.product_options(only_active=False):
        for sku, name, cat, unit, cost, price, spec in PRODUCT_SEED:
            master.save_product({
                "sku": sku, "name": name, "category_id": cats.get(cat), "spec": spec,
                "unit": unit, "cost_price": cost, "sale_price": price,
                "safety_stock": round(random.uniform(20, 120), 0),
                "lead_time_days": random.choice([5, 7, 10, 14, 21]),
            })
            stat["products"] += 1

    if not master.list_partners():
        for code, name, contact, phone, addr, terms, limit in SUPPLIERS:
            master.save_partner({"code": code, "name": name, "type": "SUPPLIER", "contact": contact,
                                 "phone": phone, "address": addr, "payment_terms_days": terms,
                                 "credit_limit": limit})
            stat["partners"] += 1
        for code, name, contact, phone, addr, terms, limit in CUSTOMERS:
            master.save_partner({"code": code, "name": name, "type": "CUSTOMER", "contact": contact,
                                 "phone": phone, "address": addr, "payment_terms_days": terms,
                                 "credit_limit": limit})
            stat["partners"] += 1

    for u, full, role in DEMO_USERS:
        auth.create_user(u, full, role, "123456")

    products = master.product_options()
    suppliers = master.list_partners(ptype="SUPPLIER")
    customers = master.list_partners(ptype="CUSTOMER")
    warehouses = master.list_warehouses()
    wh = warehouses[0]["id"]
    today_d = datetime.date.today()

    # ---------------- 按月份生成业务 ----------------
    for mi, ms in enumerate(_month_starts(months)):
        ndays = calendar.monthrange(ms.year, ms.month)[1]
        growth = 0.7 + mi * 0.12          # 越近的月份业务量越大

        # 采购：月初下单，月中到货
        for _ in range(random.randint(4, 7)):
            sup = random.choice(suppliers)
            order_day = random.randint(1, 6)
            d1 = datetime.date(ms.year, ms.month, order_day)
            if d1 >= today_d:
                continue
            items = []
            for p in random.sample(products, random.randint(3, 6)):
                qty = round(random.uniform(60, 220) * growth, 0)
                price = round(float(p["cost_price"]) * random.uniform(0.92, 1.10), 2)
                items.append({"product_id": p["id"], "qty": qty, "unit_price": price})
            ok, ref = purchase.save_order(
                {"supplier_id": sup["id"], "warehouse_id": wh, "order_date": d1.isoformat(),
                 "expected_date": (d1 + datetime.timedelta(days=7)).isoformat(),
                 "remark": "常规补货"}, items, admin)
            if not ok:
                continue
            stat["po"] += 1
            oid = _find_po(ref)
            purchase.confirm_order(oid, admin)
            recv_date = min(d1 + datetime.timedelta(days=random.randint(4, 10)), today_d)
            order = purchase.get_order(oid)
            receipts = {}
            for it in order["items"]:
                q = it["qty"] if random.random() > 0.08 else round(it["qty"] * 0.6, 2)
                receipts[it["id"]] = q
            purchase.receive_order(oid, receipts, admin, move_date=recv_date.isoformat())
            # 付款
            if random.random() < 0.85:
                pay_date = recv_date + datetime.timedelta(days=int(sup["payment_terms_days"]) + random.randint(-5, 15))
                if pay_date <= today_d:
                    # v1.5.1（P3-1）：过去按「明细金额之和 × 比例」直接取整，
                    # 与按行四舍五入得出的 total_amount 会差 0.01 元，
                    # 于是出厂演示数据自带「已付 > 单据金额」的异常记录。
                    # 这里以单据金额为准并取 min，保证已付永不超单额。
                    factor = random.choice([1.0, 1.0, 0.6, 0.3])
                    due = min(sum(i["qty"] * i["unit_price"] for i in order["items"]),
                              float(order["total_amount"]))
                    amount = round(due * factor, 2)
                    if amount > 0:
                        finance.add_payment({"direction": "PAY", "partner_id": sup["id"], "amount": amount,
                                             "method": random.choice(["银行转账", "承兑汇票"]),
                                             "pay_date": pay_date.isoformat(), "ref_type": "PURCHASE",
                                             "ref_id": oid, "remark": "采购付款"}, admin)
                        stat["pay"] += 1

        # 销售：随机日期下单并出库（热销品优先，留出滞销品用于呆滞分析）
        for _ in range(random.randint(8, 14)):
            cus = random.choice(customers)
            d1 = datetime.date(ms.year, ms.month, random.randint(8, min(ndays, 28)))
            if d1 >= today_d:
                continue
            stock_rows = inventory.list_stock(only_positive=True)
            if not stock_rows:
                break
            hot = [s for s in stock_rows if s["sku"] in HOT_SKUS]
            pool_src = (hot or stock_rows) if random.random() < 0.85 else stock_rows
            pool = random.sample(pool_src, min(len(pool_src), random.randint(2, 5)))
            items = []
            for s in pool:
                avail = float(s["qty"])
                want = round(random.uniform(25, 150) * growth, 0)
                qty = min(want, max(1.0, avail * random.uniform(0.25, 0.85)))
                if qty < 1:
                    continue
                price = round(float(s["sale_price"]) * random.uniform(0.90, 1.05), 2)
                items.append({"product_id": s["product_id"], "qty": qty, "unit_price": price})
            if not items:
                continue
            ok, ref = sales.save_order(
                {"customer_id": cus["id"], "warehouse_id": wh, "order_date": d1.isoformat(),
                 "deliver_date": (d1 + datetime.timedelta(days=random.randint(3, 12))).isoformat(),
                 "remark": "客户订单"}, items, admin)
            if not ok:
                continue
            stat["so"] += 1
            oid = _find_so(ref)
            okc, msg = sales.confirm_order(oid, admin, force=True)
            if not okc:
                sales.delete_order(oid, admin)
                stat["so"] -= 1
                continue
            order = sales.get_order(oid)
            send_date = min(d1 + datetime.timedelta(days=random.randint(1, 8)), today_d)
            delivers = {}
            for it in order["items"]:
                q = it["qty"] if random.random() > 0.1 else round(it["qty"] * 0.7, 2)
                delivers[it["id"]] = q
            okd, _ = sales.deliver_order(oid, delivers, admin, move_date=send_date.isoformat())
            if not okd:
                continue
            # 收款
            if random.random() < 0.8:
                pay_date = send_date + datetime.timedelta(days=int(cus["payment_terms_days"]) + random.randint(-10, 25))
                if pay_date <= today_d:
                    order = sales.get_order(oid)
                    # 同采购侧：以单据金额为准，避免分位累积出 0.01 元的超收
                    factor = random.choice([1.0, 1.0, 0.5, 0.3])
                    due = min(sum(i["delivered_qty"] * i["unit_price"] for i in order["items"]),
                              float(order["total_amount"]))
                    amount = round(due * factor, 2)
                    if amount > 0:
                        finance.add_payment({"direction": "RECEIVE", "partner_id": cus["id"], "amount": amount,
                                             "method": random.choice(["银行转账", "承兑汇票", "电汇"]),
                                             "pay_date": pay_date.isoformat(), "ref_type": "SALE",
                                             "ref_id": oid, "remark": "销售回款"}, admin)
                        stat["pay"] += 1

    # ---------------- 派生指标 ----------------
    inventory.recalc_reorder_points(admin)
    reports.abc_classify()

    # 演示库区流转：把少量存货转入退货区/残次区，使仓库可视化呈现多区真实分布
    try:
        zs = {z["zone_type"]: z["id"] for z in zone_svc.list_zones(wh)}
        stock = inventory.list_stock(warehouse_id=wh, only_positive=True)[:9]
        for s in stock[:5]:
            q = min(float(s["qty"]) * 0.15, 30)
            if q >= 1 and "RETURN" in zs:
                zone_svc.transfer_zone(s["product_id"], wh, zs["STORAGE"], zs["RETURN"],
                                       round(q, 1), admin, remark="演示销售退货")
        for s in stock[5:7]:
            q = min(float(s["qty"]) * 0.1, 20)
            if q >= 1 and "DEFECT" in zs:
                zone_svc.transfer_zone(s["product_id"], wh, zs["STORAGE"], zs["DEFECT"],
                                       round(q, 1), admin, remark="演示残次")
    except Exception as e:
        if verbose:
            print(f"[演示数据] 库区流转演示跳过：{e}")

    auth.write_log(admin, "生成演示数据", "系统", "", f"商品{stat['products']} 采购单{stat['po']} 销售单{stat['so']}")

    if verbose:
        print(f"[演示数据] 商品 {stat['products']}，往来单位 {stat['partners']}，"
              f"采购单 {stat['po']}，销售单 {stat['so']}，收付款 {stat['pay']}")
    return stat


def _find_po(no: str) -> int:
    rows = purchase.list_orders(keyword=no, limit=1)
    return rows[0]["id"] if rows else 0


def _find_so(no: str) -> int:
    rows = sales.list_orders(keyword=no, limit=1)
    return rows[0]["id"] if rows else 0
