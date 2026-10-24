# -*- coding: utf-8 -*-
"""Web 后台（纯标准库 http.server，无需任何第三方依赖）

启动：python main.py --web   访问 http://127.0.0.1:8765
"""
import csv
import html
import io
import secrets
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import db
from .services import auth, master, inventory, purchase, sales, finance, reports, returns as rt_svc
from .services import importer, warehouse_viz, zones as zone_svc, marketplace as mp_svc
from .config import MARKETPLACE
from .config import (APP_NAME, APP_VERSION, MODULES, ROLE_NAMES, ROLE_PERMS, can,
                     effective_perms, PERM_READ, PERM_WRITE, PERM_NONE,
                     PO_STATUS, SO_STATUS, RT_STATUS, RT_TYPE, PARTNER_TYPE, PAY_DIRECTION,
                     ABC_CLASS, PAY_METHOD)
from .utils import money, num, pct, today, safe_float, add_days

SESSIONS = {}
# 操作结果提示（flash）：{会话令牌: (ok|err, 文本)}。
# POST 处理后写入，下一次 GET 取出并渲染后即销毁，避免刷新重复提示。
FLASH = {}

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:"Microsoft YaHei UI","PingFang SC",Segoe UI,sans-serif;background:#F5F6F8;color:#1F2937;font-size:14px}
a{color:#9B2C2C;text-decoration:none}
a:hover{text-decoration:underline}
/* ---------- 顶部站点头（品牌区） ---------- */
.hdr{background:#5A1010;color:#fff}
.hdr-in{max-width:1340px;margin:0 auto;padding:12px 24px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:10px;font-size:17px;font-weight:500;color:#fff}
.brand .mark{width:28px;height:28px;border-radius:8px;flex:none;display:block;object-fit:contain;background:#8B1A1A}
.brand .ver{font-size:12px;color:#C99B9B;font-weight:400}
.hdr-r{margin-left:auto;display:flex;align-items:center;gap:16px;font-size:13px;color:#E8CACA}
.hdr-r .who{color:#fff}
.hdr-r a{color:#E8CACA}
.hdr-r a:hover{color:#fff}
/* ---------- 顶部导航条（板块在顶端） ---------- */
.nav{background:#fff;border-bottom:1px solid #E5E7EB;box-shadow:0 1px 3px rgba(0,0,0,0.04)}
.nav-in{max-width:1340px;margin:0 auto;padding:0 24px;display:flex;flex-wrap:wrap;gap:2px}
.nav a{padding:13px 14px;font-size:14px;color:#4B5563;border-bottom:3px solid transparent}
.nav a:hover{background:#FBF7F7;color:#701414;text-decoration:none}
.nav a.on{background:#FBF7F7;color:#701414;font-weight:500;border-bottom-color:#8B1A1A}
/* ---------- 内容容器（居中，普通网站观感） ---------- */
.container{max-width:1340px;margin:0 auto;padding:22px 24px}
.pghead{display:flex;align-items:baseline;flex-wrap:wrap;gap:10px;margin-bottom:16px}
.pghead h2{font-size:19px;font-weight:500}
.pghead .desc{color:#6B7280;font-size:13px}
.pghead .date{margin-left:auto;color:#6B7280;font-size:13px}
.pagefoot{max-width:1340px;margin:0 auto;padding:2px 24px 26px;color:#9CA3AF;font-size:12px;text-align:center}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:14px}
.card{background:#fff;border:1px solid #E5E7EB;border-radius:12px;padding:16px 18px;flex:1;min-width:150px;box-shadow:0 1px 3px rgba(0,0,0,0.05)}
.card .k{color:#6B7280;font-size:12px}
.card .v{font-size:22px;font-weight:500;margin-top:4px;color:#701414}
.card .s{color:#9CA3AF;font-size:12px;margin-top:2px}
.panel{background:#fff;border:1px solid #E5E7EB;border-radius:12px;padding:16px;margin-bottom:14px;box-shadow:0 1px 3px rgba(0,0,0,0.05)}
.panel h3{font-size:15px;margin-bottom:10px;font-weight:500}
table{width:100%;border-collapse:collapse;font-size:13px}
th{background:#F2DEDE;text-align:left;padding:9px 10px;font-weight:500;white-space:nowrap;color:#701414}
td{padding:8px 10px;border-top:1px solid #F0E2E2;white-space:nowrap}
tr:nth-child(even) td{background:#FBF7F7}
tr:hover td{background:#FCEFEF}
.r{text-align:right}.c{text-align:center}
.tag{display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px}
.ok{color:#16A34A}.warn{color:#D97706}.bad{color:#DC2626}.muted{color:#9CA3AF}
.bar{background:#F0DADA;border-radius:4px;height:14px;position:relative;min-width:60px}
.bar i{display:block;background:#8B1A1A;height:14px;border-radius:4px}
.toolbar{display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap}
input,select,button{font-family:inherit;font-size:13px;padding:7px 10px;border:1px solid #D1D5DB;border-radius:8px;background:#fff;color:#1F2937}
input:focus,select:focus{outline:none;border-color:#9B2C2C;box-shadow:0 0 0 3px rgba(155,44,44,0.12)}
button{background:#8B1A1A;color:#fff;border-color:#8B1A1A;cursor:pointer;border-radius:8px}
button:hover{background:#701414}
button.gray{background:#fff;color:#374151;border-color:#D1D5DB}
button.gray:hover{background:#F3F4F6}
button.red{background:#DC2626;border-color:#DC2626}
button.red:hover{background:#B91C1C}
button.green{background:#16A34A;border-color:#16A34A}
button.green:hover{background:#15803D}
.msg{background:#FCEFEF;border:1px solid #E8CACA;color:#701414;padding:10px 14px;border-radius:8px;margin-bottom:12px}
.flash{padding:11px 14px;border-radius:8px;margin:0 0 14px;font-size:14px}
.flash.ok{background:#ECFDF5;border:1px solid #A7F3D0;color:#065F46}
.flash.err{background:#FEF2F2;border:1px solid #FECACA;color:#991B1B}
.two{display:flex;gap:14px}.two>div{flex:1}
.login{width:360px;margin:64px auto;background:#fff;border:1px solid #E5E7EB;border-radius:14px;padding:30px;box-shadow:0 4px 16px rgba(0,0,0,0.08)}
.login h2{font-size:20px;margin-bottom:6px;font-weight:500}.login p{color:#6B7280;font-size:13px;margin-bottom:20px}
.login input{width:100%;margin-bottom:14px}
.login button{width:100%;padding:10px;font-size:15px}
.foot{color:#9CA3AF;font-size:12px;margin-top:16px;line-height:1.7}
.btn{display:inline-block;padding:9px 16px;border-radius:8px;background:#8B1A1A;color:#fff;font-size:14px}
.btn:hover{background:#701414;color:#fff;text-decoration:none}
.btn.gray{background:#fff;color:#374151;border:1px solid #D1D5DB}
.btn.gray:hover{background:#F3F4F6;color:#374151}
.iobar{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.iobar span{color:#6B7280;font-size:12.5px}
/* ---------- 移动端适配：手机竖屏（APK 内 WebView 与手机浏览器） ---------- */
@media (max-width:760px){
.hdr-in{padding:10px 14px;gap:8px}
.brand{font-size:16px;gap:8px}
.brand .mark{width:26px;height:26px}
.brand .ver{display:none}
.hdr-r{gap:12px;font-size:12px}
.nav-in{padding:0 6px;flex-wrap:nowrap;overflow-x:auto;-webkit-overflow-scrolling:touch}
.nav a{padding:12px 12px;font-size:13px;white-space:nowrap}
.container{padding:14px 12px}
.pghead{gap:6px;margin-bottom:12px}
.pghead h2{font-size:17px}
.pghead .date{margin-left:0;width:100%;font-size:12px}
.pagefoot{padding:2px 12px 20px}
.cards{flex-direction:column;gap:10px;margin-bottom:10px}
.card{min-width:0;width:100%;padding:14px 16px;border-radius:10px}
.panel{padding:12px;border-radius:10px;margin-bottom:10px}
.panel h3{font-size:14px}
table{display:block;width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch}
thead,tbody{display:table;width:100%}
th,td{padding:8px 10px}
.toolbar{gap:6px}
input,select,button{font-size:16px;padding:10px 12px}
button{padding:10px 14px}
.two{flex-direction:column}
.two>div{flex:none}
.login{width:auto;max-width:360px;margin:24px 12px;padding:22px}
.login h2{font-size:18px}
.foot{font-size:11px}
}
"""


def esc(v):
    return html.escape(str(v if v is not None else ""))


_FAVICON_CACHE = {"data": None}


def _favicon_bytes():
    """读取天宫宫殿徽标（icon.png）作为站点图标，结果缓存。

    源码运行取 erp/ui/assets/；PyInstaller 打包时随 --add-data
    落在 <_MEIPASS>/erp/ui/assets/。资源缺失时返回 None（浏览器显示默认图标）。
    """
    if _FAVICON_CACHE["data"] is not None:
        return _FAVICON_CACHE["data"]
    try:
        if getattr(sys, "frozen", False):
            base = Path(getattr(sys, "_MEIPASS", "")) / "erp" / "ui"
        else:
            base = Path(__file__).parent / "ui"
        data = (base / "assets" / "icon.png").read_bytes()
    except OSError:
        data = b""
    _FAVICON_CACHE["data"] = data or None
    return _FAVICON_CACHE["data"]


def layout(user, active, title, desc, body, flash=""):
    navs = []
    for key, label in MODULES:
        if not can(user, key):
            continue
        cls = "on" if key == active else ""
        navs.append(f'<a class="{cls}" href="/{key}">{label}</a>')
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<link rel="icon" type="image/png" href="/favicon.ico">
<title>{esc(title)} · {APP_NAME}</title><style>{CSS}</style></head><body>
<header class="hdr"><div class="hdr-in">
<div class="brand"><img class="mark" src="/logo.png" alt="天宫">{APP_NAME}<span class="ver">v{APP_VERSION}</span></div>
<div class="hdr-r"><span class="who">{esc(user.get('full_name') or user.get('username'))}（{esc(ROLE_NAMES.get(user.get('role'), ''))}）</span>
<a href="/password">修改口令</a><a href="/logout">退出登录</a></div>
</div></header>
<nav class="nav"><div class="nav-in">{''.join(navs)}</div></nav>
<div class="container">
<div class="pghead"><h2>{esc(title)}</h2><span class="desc">{esc(desc)}</span>
<span class="date">{today()}</span></div>{flash}{body}</div>
<div class="pagefoot">采购 · 销售 · 库存 · 资金 一体化管理 · 天宫ERP系统 v{APP_VERSION}</div>
</body></html>"""


def card(k, v, s=""):
    return f'<div class="card"><div class="k">{esc(k)}</div><div class="v">{esc(v)}</div><div class="s">{esc(s)}</div></div>'


def table(headers, rows, aligns=None):
    aligns = aligns or []
    th = "".join(f'<th class="{a}">' + esc(h) + "</th>" for h, a in zip(headers, aligns + [""] * len(headers)))
    trs = []
    for r in rows:
        tds = "".join(f'<td class="{a}">{c}</td>' for c, a in zip(r, aligns + [""] * len(r)))
        trs.append(f"<tr>{tds}</tr>")
    if not trs:                      # 空态显式提示，避免只剩表头像页面出错
        trs.append(f'<tr><td colspan="{len(headers)}" class="muted" '
                   f'style="padding:18px;text-align:center">暂无数据</td></tr>')
    return f'<div class="panel"><table><thead><tr>{th}</tr></thead><tbody>{"".join(trs)}</tbody></table></div>'


def search_form(action="/", placeholder="关键字"):
    return (f'<form class="toolbar" method="get" action="{action}">'
            f'<input name="q" placeholder="{placeholder}" value="">'
            f'<button class="gray" type="submit">搜索</button></form>')


# ================================================================ 页面
def _io_bar():
    """数据交换入口条：批量导入 / 数据导出（手机与电脑互倒数据）。

    单独抽出是为了仪表盘与导入、导出页共用，避免用户在手机上找不到入口。
    """
    return ('<div class="panel"><div class="iobar">'
            '<a class="btn" href="/import">批量导入</a>'
            '<a class="btn gray" href="/export">数据导出</a>'
            '<span>与电脑版互倒数据：电脑导出 CSV → 手机粘贴导入；'
            '手机导出 CSV → 全选复制回电脑</span>'
            '</div></div>')


def page_dashboard(user, p):
    d = reports.dashboard()
    s30 = d["sales_30"]
    body = _io_bar()
    body += '<div class="cards">' + "".join([
        card("本月销售额(近30天)", money(s30["revenue"]), f'{num(s30["qty"])} 件 / {s30["orders"]} 单'),
        card("本月毛利", money(s30["gross"]), f'成本 {money(s30["cogs"])}'),
        card("毛利率", pct(s30["margin"]), f'目标 {pct(d["gross_margin_target"])}'),
        card("库存资产", money(d["stock_value"]), f'{d["sku_count"]} SKU / {num(d["stock_qty"])} 件'),
    ]) + "</div>"
    body += '<div class="cards">' + "".join([
        card("库存周转率(年化)", f'{d["turnover"]:.2f} 次', f'COGS {money(d["cogs_90"])}'),
        card("库存周转天数 DIO", f'{d["dio"]:.1f} 天', "365 ÷ 周转率"),
        card("应收周转 DSO", f'{d["dso"]:.1f} 天', "应收 ÷ 日均销售"),
        card("应付周转 DPO", f'{d["dpo"]:.1f} 天', "应付 ÷ 日均成本"),
        card("现金周期 CCC", f'{d["ccc"]:.1f} 天', "DIO + DSO − DPO"),
    ]) + "</div>"
    body += '<div class="cards">' + "".join([
        card("应收余额", money(d["ar_balance"]), f'已收 {money(d["cash_in"])}'),
        card("应付余额", money(d["ap_balance"]), f'已付 {money(d["cash_out"])}'),
        card("净现金流", money(d["net_cash"]), "收款 − 付款"),
        card("缺货 SKU", f'{d["sku_shortage"]} 个', f'缺货率 {pct(d["stockout_rate"])}'),
        card("呆滞库存", money(d["dull_value"]), f'{d["dull_skus"]} 个 SKU'),
    ]) + "</div>"

    mx = max([m["revenue"] for m in d["monthly"]] or [1])
    bars = "".join(
        f'<tr><td>{m["ym"]}</td><td class="r">{money(m["revenue"])}</td>'
        f'<td class="r">{money(m["gross"])}</td><td class="r">{pct(m["margin"])}</td>'
        f'<td><div class="bar"><i style="width:{m["revenue"] / mx * 100:.1f}%"></i></div></td></tr>'
        for m in d["monthly"])
    body += ('<div class="two"><div class="panel"><h3>近 6 个月销售趋势</h3><table>'
             '<tr><th>月份</th><th class="r">收入</th><th class="r">毛利</th><th class="r">毛利率</th><th>占比</th></tr>'
             + bars + "</table></div>")
    tops = "".join(f'<tr><td>{esc(t["sku"])}</td><td>{esc(t["name"])}</td>'
                   f'<td class="r">{money(t["revenue"])}</td><td class="r">{pct(t["margin"])}</td></tr>'
                   for t in d["top_products"])
    body += ('<div class="panel"><h3>热销 TOP5</h3><table>'
             '<tr><th>SKU</th><th>商品</th><th class="r">销售额</th><th class="r">毛利率</th></tr>'
             + tops + "</table></div></div>")

    short = "".join(f'<tr><td>{esc(s["sku"])}</td><td>{esc(s["name"])}</td>'
                    f'<td class="r">{num(s["qty"])}</td><td class="r">{num(s["safety_stock"])}</td>'
                    f'<td class="r">{num(s["rop"])}</td></tr>' for s in d["shortage"]) or \
            '<tr><td colspan="5" class="muted">暂无缺货</td></tr>'
    body += ('<div class="panel"><h3>⚠ 缺货预警</h3><table>'
             '<tr><th>SKU</th><th>商品</th><th class="r">库存</th><th class="r">安全库存</th><th class="r">再订货点</th></tr>'
             + short + "</table></div>")
    return body


def page_products(user, p):
    q = p.get("q", "")
    rows = master.list_products(keyword=q)
    body = search_form("/products", "SKU / 名称 / 规格")
    if can(user, "products", True):
        body += ('<div class="panel"><h3>新增 / 修改商品</h3><form method="post" action="/do/product_save">'
                 '<div class="toolbar">'
                 '<input name="sku" placeholder="SKU *" required>'
                 '<input name="name" placeholder="名称 *" required>'
                 '<input name="unit" placeholder="单位" value="件" style="width:70px">'
                 '<input name="cost_price" placeholder="成本价" style="width:90px">'
                 '<input name="sale_price" placeholder="销售价" style="width:90px">'
                 '<input name="safety_stock" placeholder="安全库存" style="width:90px">'
                 '<input name="lead_time_days" placeholder="提前期" value="7" style="width:80px">'
                 '<button type="submit">保存</button></div></form></div>')
    data = [(esc(r["sku"]), esc(r["name"]), esc(r.get("category_name") or "-"), esc(r.get("spec") or ""),
             esc(r["unit"]), money(r["cost_price"]), money(r["sale_price"]),
             num(r.get("stock_qty", 0)), money(r.get("stock_value", 0)),
             num(r["safety_stock"]), num(r.get("reorder_point", 0)), esc(r["abc_class"]),
             '<span class="ok">启用</span>' if r["active"] else '<span class="muted">停用</span>')
            for r in rows]
    return body + table(["SKU", "名称", "分类", "规格", "单位", "成本价", "销售价", "库存",
                         "库存金额", "安全库存", "ROP", "ABC", "状态"], data,
                        ["", "", "", "", "c", "r", "r", "r", "r", "r", "r", "c", "c"])


def page_partners(user, p):
    q = p.get("q", "")
    rows = master.list_partners(keyword=q)
    body = search_form("/partners", "编码 / 名称 / 联系人")
    if can(user, "partners", True):
        body += ('<div class="panel"><h3>新增往来单位</h3><form method="post" action="/do/partner_save">'
                 '<div class="toolbar">'
                 '<input name="code" placeholder="编码 *" required>'
                 '<input name="name" placeholder="名称 *" required>'
                 '<select name="type">' + "".join(f'<option value="{k}">{v}</option>' for k, v in PARTNER_TYPE.items()) + '</select>'
                 '<input name="contact" placeholder="联系人">'
                 '<input name="phone" placeholder="电话">'
                 '<input name="payment_terms_days" placeholder="账期" value="30" style="width:80px">'
                 '<input name="credit_limit" placeholder="信用额度" style="width:100px">'
                 '<input name="remark" placeholder="备注" style="width:120px">'
                 '<button type="submit">保存</button></div></form></div>')
    data = []
    for r in rows:
        ar = finance.partner_balance(r["id"], "AR") if r["type"] in ("CUSTOMER", "BOTH") else 0
        ap = finance.partner_balance(r["id"], "AP") if r["type"] in ("SUPPLIER", "BOTH") else 0
        data.append((esc(r["code"]), esc(r["name"]), esc(PARTNER_TYPE.get(r["type"], "")),
                     esc(r.get("contact") or ""), esc(r.get("phone") or ""),
                     str(r["payment_terms_days"]), money(r["credit_limit"]),
                     esc(r.get("remark") or ""), money(ar), money(ap),
                     '<span class="ok">启用</span>' if r["active"] else '<span class="muted">停用</span>'))
    return body + table(["编码", "名称", "类型", "联系人", "电话", "账期", "信用额度",
                         "备注", "应收余额", "应付余额", "状态"], data,
                        ["", "", "", "", "", "c", "r", "", "r", "r", "c"])


def page_inventory(user, p):
    tab = p.get("tab", "stock")
    tabs = f'<div class="toolbar"><a href="/inventory?tab=stock"><button class="gray">库存余额</button></a>' \
           f'<a href="/inventory?tab=moves"><button class="gray">出入库流水</button></a>' \
           f'<a href="/inventory?tab=alert"><button class="gray">缺货预警</button></a></div>'
    if tab == "moves":
        rows = inventory.list_moves(keyword=p.get("q", ""), limit=300)
        data = [(esc(r["move_no"]), esc(r["move_date"]), esc(r["move_type"]), esc(r["sku"]),
                 esc(r["product_name"]), esc(r["warehouse_name"]),
                 f'{("+" if r["direction"] > 0 else "-")}{float(r["qty"]):g}',
                 money(r["unit_cost"]), money(r["amount"]), num(r["balance_after"]),
                 esc(r.get("ref_no") or "-")) for r in rows]
        return tabs + search_form("/inventory?tab=moves", "SKU / 商品 / 单号") + \
            table(["流水号", "日期", "类型", "SKU", "商品", "仓库", "数量", "单位成本",
                   "金额", "结存", "来源"], data, ["", "", "c", "", "", "", "r", "r", "r", "r", ""])
    if tab == "alert":
        rows = inventory.low_stock_alerts()
        data = [(esc(r["sku"]), esc(r["name"]), esc(r["warehouse_name"]), num(r["qty"]),
                 num(r["safety_stock"]), num(float(r["safety_stock"]) - float(r["qty"])),
                 num(r["reorder_point"]), str(r["lead_time_days"])) for r in rows]
        return tabs + table(["SKU", "商品", "仓库", "库存", "安全库存", "缺口", "ROP", "提前期"],
                            data, ["", "", "", "r", "r", "r", "r", "c"])
    rows = inventory.list_stock(keyword=p.get("q", ""))
    data = [(esc(r["sku"]), esc(r["name"]), esc(r["warehouse_name"]), num(r["qty"]),
             money(r["avg_cost"]), money(r["stock_value"]), money(r["retail_value"]),
             num(r["safety_stock"]), num(r["reorder_point"]), esc(r.get("updated_at") or "-"))
            for r in rows]
    return tabs + search_form("/inventory", "SKU / 名称") + \
        table(["SKU", "商品", "仓库", "库存量", "加权成本", "库存金额", "零售估值",
               "安全库存", "ROP", "更新时间"], data, ["", "", "", "r", "r", "r", "r", "r", "r", ""])


def page_warehouse(user, p):
    """仓库可视化：平面分布 + 库区实时库存 + 钻取下探 + 库内移库（v1.6.0）"""
    whs = master.list_warehouses()
    if not whs:
        return '<div class="msg">尚未配置仓库，请先到「库存管理」或联系管理员建立仓库。</div>'
    try:
        wh = int(p.get("wh")) if p.get("wh") else whs[0]["id"]
    except (TypeError, ValueError):
        wh = whs[0]["id"]
    sel = '<form class="toolbar" method="get" action="/warehouse"><select name="wh" onchange="this.form.submit()">' + \
          "".join(f'<option value="{w["id"]}"{" selected" if w["id"]==wh else ""}>{esc(w["name"])}</option>'
                  for w in whs) + '</select></form>'

    zone_id = p.get("zone")
    body = sel
    body += '<div class="panel">' + warehouse_viz.build_floorplan_svg(wh) + '</div>'

    if zone_id and str(zone_id).isdigit():
        z = zone_svc.get_zone(int(zone_id))
        if z and int(z["warehouse_id"]) == wh:
            body += f'<div class="toolbar"><a href="/warehouse?wh={wh}"><button class="gray">← 返回平面图</button></a></div>'
            body += f'<div class="panel">{warehouse_viz.zone_detail_svg(int(zone_id))}</div>'

    # 库内移库（需库存写权限）
    if can(user, "inventory", True):
        zlist = zone_svc.list_zones(wh)
        opts = "".join(f'<option value="{z["id"]}">{esc(z["name"])}</option>' for z in zlist)
        body += ('<div class="panel"><h3>库内移库（如：销售退货转入退货区）</h3>'
                 '<form class="toolbar" method="post" action="/do/zone_transfer">'
                 f'<input name="wh" value="{wh}" type="hidden">'
                 '<input name="sku" placeholder="商品SKU" style="width:120px">'
                 '<select name="from_zone">' + opts + '</select>'
                 '<span>→</span>'
                 '<select name="to_zone">' + opts + '</select>'
                 '<input name="qty" placeholder="数量" style="width:90px">'
                 '<button type="submit">移库</button></form>'
                 '<div class="foot">移库不改变仓库总库存，仅在各库区之间重分类；流水记入库存台账，分区合计始终与进销存一致。</div></div>')
    return body


def _doc_rows(rows, no_key, done_key, status_map, link):
    data = []
    for r in rows:
        pctv = (float(r[done_key]) / float(r["total_qty"]) * 100) if r["total_qty"] else 0
        data.append((f'<a href="{link}?id={r["id"]}">{esc(r[no_key])}</a>', esc(r["order_date"]),
                     esc(r["partner_name"]), esc(r["warehouse_name"]), num(r["total_qty"]),
                     num(r[done_key]), f'{pctv:.0f}%', money(r["total_amount"]),
                     money(r["paid_amount"]), money(float(r["total_amount"]) - float(r["paid_amount"])),
                     esc(status_map.get(r["status"], r["status"])), esc(r.get("created_by") or "-")))
    return data


DOC_HEAD = ["单据号", "日期", "往来单位", "仓库", "数量", "已执行", "进度", "金额",
            "已结算", "未结算", "状态", "制单人"]
DOC_ALIGN = ["", "", "", "", "r", "r", "c", "r", "r", "r", "c", ""]


def page_purchase(user, p):
    rows = purchase.list_orders(keyword=p.get("q", ""), status=p.get("status", ""))
    body = search_form("/purchase", "单号 / 供应商")
    return body + table(DOC_HEAD, _doc_rows(rows, "po_no", "recv_qty", PO_STATUS, "/po"), DOC_ALIGN)


def page_sales(user, p):
    rows = sales.list_orders(keyword=p.get("q", ""), status=p.get("status", ""))
    body = search_form("/sales", "单号 / 客户")
    return body + table(DOC_HEAD, _doc_rows(rows, "so_no", "send_qty", SO_STATUS, "/so"), DOC_ALIGN)


# ============================================================ 退货（v2.2.0）
def _rt_rows(rows):
    data = []
    for r in rows:
        data.append((
            f'<a href="/rt?id={r["id"]}">{esc(r["rt_no"])}</a>',
            esc(RT_TYPE.get(r["rt_type"], r["rt_type"])),
            esc(r["ref_no"] or "-"),
            esc(r["partner_name"]),
            esc(r["warehouse_name"]),
            num(r["total_qty"]),
            money(r["total_amount"]),
            esc(RT_STATUS.get(r["status"], r["status"])),
            esc(r["order_date"]),
            esc(r.get("created_by") or "-"),
        ))
    return data


RT_HEAD = ["退货单号", "类型", "关联原单", "往来单位", "仓库", "数量", "退货金额", "状态", "日期", "制单人"]
RT_ALIGN = ["", "", "", "", "", "r", "r", "c", "", ""]


def page_returns(user, p):
    rt_type = p.get("rt", "")
    rt_type_filter = {"sale": "SALE_RETURN", "purchase": "PURCHASE_RETURN"}.get(rt_type, "")
    rows = rt_svc.list_orders(rt_type=rt_type_filter, keyword=p.get("q", ""), status=p.get("status", ""))
    tabs = ('<div class="toolbar">'
            '<a href="/returns?rt="><button class="gray">全部</button></a>'
            '<a href="/returns?rt=sale"><button class="gray">销售退货</button></a>'
            '<a href="/returns?rt=purchase"><button class="gray">采购退货</button></a></div>')
    # 快速新建：选类型 + 关联原单号，自动按可退量整单退（精确逐行退货请在桌面端）
    w = can(user, "returns", True)
    new_form = ""
    if w:
        so_opts = "".join(f'<option value="SO:{o["id"]}">{esc(o["so_no"])} · {esc(o["partner_name"])}</option>'
                          for o in sales.list_orders())
        po_opts = "".join(f'<option value="PO:{o["id"]}">{esc(o["po_no"])} · {esc(o["partner_name"])}</option>'
                          for o in purchase.list_orders())
        new_form = ('<div class="panel"><h3>快速新建退货单（整单退回，按可退量自动生成明细）</h3>'
                    '<form class="toolbar" method="post" action="/do/return_save">'
                    '<select name="rt_type"><option value="SALE_RETURN">销售退货</option>'
                    '<option value="PURCHASE_RETURN">采购退货</option></select>'
                    '<select name="ref_key"><optgroup label="销售单">' + so_opts + '</optgroup>'
                    '<optgroup label="采购单">' + po_opts + '</optgroup></select>'
                    '<input name="remark" placeholder="备注" style="width:160px">'
                    '<button type="submit">生成退货单</button></form>'
                    '<div class="foot">Web 端按「整单可退量」快速生成；如需部分退货或逐行改单价，请在桌面端录入。</div></div>')
    body = tabs + new_form + search_form("/returns", "单号 / 往来单位 / 备注")
    return body + table(RT_HEAD, _rt_rows(rows), RT_ALIGN)


def page_return_detail(user, p):
    rid = int(p.get("id", 0) or 0)
    order = rt_svc.get_order(rid)
    if not order:
        return '<div class="msg">退货单不存在</div>'
    body = '<div class="cards">' + "".join([
        card("退货单号", order["rt_no"]),
        card("类型", RT_TYPE.get(order["rt_type"], order["rt_type"])),
        card("关联原单", order.get("ref_no") or "-"),
        card("往来单位", order["partner_name"]),
        card("仓库", order["warehouse_name"]),
        card("状态", RT_STATUS.get(order["status"], order["status"])),
        card("退货金额", money(order["total_amount"])),
    ]) + "</div>"
    data = [(esc(i["sku"]), esc(i["product_name"]), num(i["qty"]), money(i["unit_price"]),
             money(i["amount"])) for i in order["items"]]
    body += table(["SKU", "商品", "退货数量", "单价", "金额"], data, ["", "", "r", "r", "r"])
    w = can(user, "returns", True)
    act = []
    if w and order["status"] == "DRAFT":
        act.append(f'<button class="green" name="a" value="confirm">确认</button>')
    if w and order["status"] in ("DRAFT", "CONFIRMED"):
        act.append(f'<button name="a" value="post">过账执行</button>')
    if w and order["status"] != "DONE":
        act.append(f'<button class="red" name="a" value="cancel">取消</button>')
    if act:
        body += (f'<form class="panel" method="post" action="/do/return_action">'
                 f'<input type="hidden" name="id" value="{rid}">'
                 f'<div class="toolbar">{"".join(act)}</div></form>')
    return body


# ================================================================ 市场对接
def page_marketplace(user, p):
    w = can(user, "marketplace", True)
    ch = p.get("ch", "")
    # ---- 快速开始（常驻，不按权限隐藏，方便只读账号也了解对接流程）----
    guide = ('<div class="panel info"><h3>快速开始 · 市场对接三步</h3><ol>'
             '<li><b>配置渠道凭证</b>：在「渠道凭证」区点【保存凭证】，选 SHOPIFY / TIKTOK，'
             '填店铺域名与 Access Token，保存后在列表选中该渠道点【激活】。令牌仅存本地库；停用即停止拉单。</li>'
             '<li><b>维护 SKU 映射</b>：在「SKU 映射」区点【新增映射】，把平台 SKU 对应到本地商品并勾选启用。'
             '缺映射的订单会在下方日志记为失败，可查看原因。</li>'
             '<li><b>拉单 / 回写</b>：点顶部【同步全部已激活渠道订单】把平台新订单落地为销售单'
             '（自动出库、库存联动、多币种）；点【回写库存到平台】把本仓可用库存回写平台可售数。</li>'
             '</ol><div class="foot">提示：需 marketplace 写权限账号（默认 admin）才能看到操作按钮；'
             '只读账号仅能查看列表与日志。后台也会按 config.MARKETPLACE 的间隔定时同步。</div></div>')
    # ---- 渠道凭证 ----
    creds = mp_svc.list_credentials()
    cred_rows = []
    for c in creds:
        cred_rows.append((
            esc(c["channel"]),
            "已激活" if c.get("status") == "ACTIVE" else "已停用",
            esc(c.get("shop") or "-"),
            "●" * 6 if c.get("access_token") else "（未配置）",
            esc(c.get("updated_at") or "-"),
            esc(c.get("note") or ""),
        ))
    cred_panel = table(["渠道", "状态", "店铺 / App", "访问令牌", "更新时间", "备注"],
                       cred_rows, ["", "c", "", "c", "c", ""])
    if w:
        chan_opts = "".join(f'<option value="{c}">{c}</option>' for c in MARKETPLACE["channels"])
        cred_panel += ('<div class="panel"><h3>配置 / 更新渠道凭证</h3>'
                       '<form class="toolbar" method="post" action="/do/mp_cred_save">'
                       f'<select name="channel">{chan_opts}</select>'
                       '<select name="status"><option value="ACTIVE">激活</option>'
                       '<option value="INACTIVE">停用</option></select>'
                       '<input name="shop" placeholder="店铺域名 / App" style="width:170px">'
                       '<input name="access_token" placeholder="Access Token" style="width:180px">'
                       '<input name="refresh_token" placeholder="Refresh Token" style="width:180px">'
                       '<input name="note" placeholder="备注" style="width:120px">'
                       '<button type="submit">保存凭证</button></form>'
                       '<div class="foot">令牌仅存于本地数据库；停用后不再拉单。推送库存需平台侧 inventory 映射。</div></div>')

    # ---- SKU 映射 ----
    if ch:
        maps = mp_svc.list_sku_maps(channel=ch)
    else:
        maps = mp_svc.list_sku_maps()
    map_rows = [(esc(m["sku"]), esc(m["product_name"]), esc(m["channel"]),
                 esc(m["platform_sku"]), esc(m.get("logistics_code") or ""),
                 "是" if m.get("active") else "否") for m in maps]
    map_panel = table(["商品SKU", "商品名称", "渠道", "平台SKU", "物流编码", "启用"],
                      map_rows, ["", "", "c", "", "", "c"])
    if w:
        prod_opts = "".join(f'<option value="{o["id"]}">{esc(o["sku"])} · {esc(o["name"])}</option>'
                            for o in master.list_products())
        chan_opts2 = "".join(f'<option value="{c}">{c}</option>' for c in MARKETPLACE["channels"])
        map_panel += ('<div class="panel"><h3>新增 SKU 映射（platform_sku ↔ 本地商品）</h3>'
                      '<form class="toolbar" method="post" action="/do/mp_map_save">'
                      f'<select name="product_id">{prod_opts}</select>'
                      f'<select name="channel">{chan_opts2}</select>'
                      '<input name="platform_sku" placeholder="平台SKU" style="width:150px">'
                      '<input name="logistics_code" placeholder="物流编码" style="width:140px">'
                      '<label><input type="checkbox" name="active" value="1" checked> 启用</label>'
                      '<button type="submit">新增映射</button></form></div>')

    # ---- 同步日志 ----
    logs = mp_svc.list_sync_log(channel=ch, limit=MARKETPLACE.get("sync_log_limit", 200))
    log_rows = [(esc(l["channel"]), esc(l.get("biz_type") or ""), esc(l.get("platform_no") or ""),
                 esc(l.get("local_no") or ""), esc(l.get("direction") or ""),
                 "成功" if l.get("status") == "OK" else "失败",
                 esc((l.get("detail") or "")[:120]), esc(l.get("synced_at") or ""))
                for l in logs]
    log_panel = table(["渠道", "业务", "平台单号", "本地单号", "方向", "状态", "详情", "时间"],
                      log_rows, ["", "c", "", "", "c", "c", "", "c"])

    chan_tabs = ('<div class="toolbar">'
                 '<a href="/marketplace"><button class="gray">全部渠道</button></a>'
                 + "".join(f'<a href="/marketplace?ch={c}"><button class="gray">{c}</button></a>'
                           for c in MARKETPLACE["channels"]) + '</div>')

    # ---- 顶部动作条 ----
    actions = ""
    if w:
        actions = ('<div class="panel"><div class="toolbar">'
                   '<form method="post" action="/do/mp_sync" style="display:inline">'
                   '<button type="submit">同步全部已激活渠道订单</button></form>'
                   + ('<form method="post" action="/do/mp_push" style="display:inline">'
                      '<button class="green" type="submit">回写库存到平台</button></form>'
                      if MARKETPLACE.get("auto_push_inventory") else "")
                   + '</div>'
                   f'<div class="foot">后台定时同步间隔：{MARKETPLACE.get("sync_interval_min", 0)} 分钟'
                   '（<=0 仅手动触发）。同步间隔与开关见 config.MARKETPLACE。</div></div>')

    return guide + actions + chan_tabs + cred_panel + map_panel + log_panel


def _doc_detail(kind, user, p):
    oid = int(p.get("id", 0) or 0)
    svc = purchase if kind == "PURCHASE" else sales
    order = svc.get_order(oid)
    if not order:
        return '<div class="msg">单据不存在</div>'
    no = order.get("po_no") or order.get("so_no")
    status_map = PO_STATUS if kind == "PURCHASE" else SO_STATUS
    body = '<div class="cards">' + "".join([
        card("单据号", no), card("往来单位", order["partner_name"]), card("仓库", order["warehouse_name"]),
        card("状态", status_map.get(order["status"], order["status"])),
        card("金额", money(order["total_amount"])),
        card("未结算", money(float(order["total_amount"]) - float(order["paid_amount"]))),
    ]) + "</div>"
    done_key = "received_qty" if kind == "PURCHASE" else "delivered_qty"
    data = [(esc(i["sku"]), esc(i["product_name"]), num(i["qty"]), money(i["unit_price"]),
             money(i["amount"]), num(i[done_key]), num(float(i["qty"]) - float(i[done_key])))
            for i in order["items"]]
    body += table(["SKU", "商品", "数量", "单价", "金额",
                   "已入库" if kind == "PURCHASE" else "已出库", "待执行"], data,
                  ["", "", "r", "r", "r", "r", "r"])
    w = can(user, "purchase" if kind == "PURCHASE" else "sales", True)
    act = []
    if w and order["status"] == "DRAFT":
        act.append(f'<button class="green" name="a" value="confirm">确认订单</button>')
    if w and order["status"] in ("CONFIRMED", "PARTIAL", "DRAFT"):
        label = "全部收货入库" if kind == "PURCHASE" else "全部出库发货"
        act.append(f'<button name="a" value="exec">{label}</button>')
    if w and order["status"] not in ("COMPLETED", "CANCELLED"):
        act.append(f'<button class="red" name="a" value="cancel">取消订单</button>')
    if act:
        body += (f'<form class="panel" method="post" action="/do/doc_action">'
                 f'<input type="hidden" name="kind" value="{kind}">'
                 f'<input type="hidden" name="id" value="{oid}">'
                 f'<div class="toolbar">{"".join(act)}</div></form>')
    if can(user, "finance", True):
        body += ('<div class="panel"><h3>登记收付款</h3><form method="post" action="/do/pay_add">'
                 f'<input type="hidden" name="direction" value="{"PAY" if kind == "PURCHASE" else "RECEIVE"}">'
                 f'<input type="hidden" name="ref_type" value="{kind}">'
                 f'<input type="hidden" name="ref_id" value="{oid}">'
                 f'<input type="hidden" name="partner_id" value="'
                 f'{order.get("supplier_id") or order.get("customer_id")}">'
                 '<div class="toolbar">'
                 f'<input name="amount" placeholder="金额" value="{float(order["total_amount"]) - float(order["paid_amount"]):.2f}">'
                 '<select name="method">' + "".join(f'<option>{m}</option>' for m in PAY_METHOD) + '</select>'
                 f'<input name="pay_date" value="{today()}" style="width:120px">'
                 '<input name="remark" placeholder="备注">'
                 '<button type="submit">保存</button></div></form></div>')
    return body


def page_finance(user, p):
    tab = p.get("tab", "pay")
    tabs = ('<div class="toolbar">'
            '<a href="/finance?tab=pay"><button class="gray">收付款流水</button></a>'
            '<a href="/finance?tab=ar"><button class="gray">应收账龄</button></a>'
            '<a href="/finance?tab=ap"><button class="gray">应付账龄</button></a>'
            '<a href="/finance?tab=bal"><button class="gray">往来余额</button></a></div>')
    if tab in ("ar", "ap"):
        kind = "AR" if tab == "ar" else "AP"
        data_a = finance.aging(kind)
        bks = "".join(card(k, money(v)) for k, v in data_a["buckets"].items())
        body = tabs + '<div class="cards">' + bks + "</div>"
        rows = [(esc(r["no"]), esc(r["partner"]), esc(r["order_date"]), esc(r.get("due_date") or "-"),
                 money(r["amount"]), money(r["paid"]), money(r["balance"]), str(r["overdue_days"]))
                for r in data_a["detail"]]
        return body + table(["单据号", "往来单位", "单据日期", "到期日", "发生额", "已结算",
                             "余额", "逾期天数"], rows, ["", "", "", "", "r", "r", "r", "r"])
    if tab == "bal":
        rows = []
        for r in finance.partner_balances("AR"):
            rows.append((esc(r["code"]), esc(r["name"]), "应收", str(r["terms"]),
                         money(r["credit_limit"]), money(r["balance"]),
                         '<span class="bad">超额</span>' if r["over_limit"] else '<span class="ok">正常</span>'))
        for r in finance.partner_balances("AP"):
            rows.append((esc(r["code"]), esc(r["name"]), "应付", str(r["terms"]),
                         money(r["credit_limit"]), money(r["balance"]),
                         '<span class="bad">超额</span>' if r["over_limit"] else '<span class="ok">正常</span>'))
        return tabs + table(["编码", "名称", "类型", "账期", "信用额度", "余额", "状态"],
                            rows, ["", "", "c", "c", "r", "r", "c"])
    rows = finance.list_payments(keyword=p.get("q", ""), limit=500)
    data = [(esc(r["pay_no"]), esc(PAY_DIRECTION.get(r["direction"], "")), esc(r["partner_name"]),
             money(r["amount"]), esc(r.get("method") or "-"), esc(r["pay_date"]),
             esc(r.get("remark") or ""), esc(r.get("created_by") or "-")) for r in rows]
    body = tabs + search_form("/finance", "流水号 / 单位")
    if can(user, "finance", True):
        partners = master.list_partners()
        body += ('<div class="panel"><h3>登记收付款</h3><form method="post" action="/do/pay_add">'
                 '<div class="toolbar">'
                 '<select name="direction"><option value="RECEIVE">收款</option><option value="PAY">付款</option></select>'
                 '<select name="partner_id">' +
                 "".join(f'<option value="{x["id"]}">{esc(x["code"])} {esc(x["name"])}</option>' for x in partners) +
                 '</select><input name="amount" placeholder="金额">'
                 '<select name="method">' + "".join(f'<option>{m}</option>' for m in PAY_METHOD) + '</select>'
                 f'<input name="pay_date" value="{today()}" style="width:120px">'
                 '<input name="remark" placeholder="备注"><button type="submit">保存</button></div></form></div>')
    return body + table(["流水号", "方向", "往来单位", "金额", "方式", "日期", "备注", "经办人"],
                        data, ["", "c", "", "r", "", "", "", ""])


def page_reports(user, p):
    tab = p.get("tab", "trend")
    tabs = ('<div class="toolbar">'
            '<a href="/reports?tab=trend"><button class="gray">销售趋势</button></a>'
            '<a href="/reports?tab=customer"><button class="gray">客户汇总</button></a>'
            '<a href="/reports?tab=supplier"><button class="gray">供应商汇总</button></a>'
            '<a href="/reports?tab=stock"><button class="gray">库存周转</button></a></div>')
    if tab == "customer":
        rows = reports.sales_by_customer(90)
        data = [(esc(r["code"]), esc(r["name"]), str(r["orders"]), num(r["qty"]),
                 money(r["revenue"]), money(r["gross"]), pct(r["margin"])) for r in rows]
        return tabs + table(["编码", "客户", "订单数", "数量", "销售额", "毛利", "毛利率"],
                            data, ["", "", "r", "r", "r", "r", "r"])
    if tab == "supplier":
        rows = reports.purchase_by_supplier(90)
        data = [(esc(r["code"]), esc(r["name"]), str(r["orders"]), num(r["qty"]),
                 money(r["amount"])) for r in rows]
        return tabs + table(["编码", "供应商", "订单数", "收货量", "采购金额"],
                            data, ["", "", "r", "r", "r"])
    if tab == "stock":
        rows = reports.inventory_analysis()
        data = [(esc(r["sku"]), esc(r["name"]), esc(r["abc"]), num(r["qty"]), money(r["value"]),
                 f'{r["turnover"]:.2f}', f'{r["dio"]:.1f}', num(r["daily_demand"]),
                 num(r["rop"]), num(r["eoq"]),
                 '<span class="bad">缺货</span>' if r["risk"] == "缺货" else
                 ('<span class="warn">预警</span>' if r["risk"] == "预警" else '<span class="ok">正常</span>'),
                 "是" if r["dull"] else "") for r in rows]
        return tabs + table(["SKU", "商品", "ABC", "库存", "金额", "周转率", "周转天数",
                             "日均需求", "ROP", "EOQ", "风险", "呆滞"], data,
                            ["", "", "c", "r", "r", "r", "r", "r", "r", "r", "c", "c"])
    rows = reports.monthly_trend(12)
    data = [(esc(r["ym"]), str(r["orders"]), num(r["qty"]), money(r["revenue"]),
             money(r["cogs"]), money(r["gross"]), pct(r["margin"])) for r in rows]
    return tabs + table(["月份", "订单数", "数量", "销售收入", "销售成本", "毛利", "毛利率"],
                        data, ["", "r", "r", "r", "r", "r", "r"])


def perms_fields(selected=None, prefix="perm_"):
    """渲染分区权限选择矩阵（不开放 / 只读 / 可编辑）"""
    selected = selected or {}
    out = ['<table class="grid"><tr><th>分区（模块）</th>'
           '<th>不开放</th><th>只读</th><th>可编辑</th></tr>']
    for key, label in MODULES:
        cur = selected.get(key, PERM_NONE)
        cells = []
        for val in (PERM_NONE, PERM_READ, PERM_WRITE):
            checked = " checked" if cur == val else ""
            cells.append(f'<td style="text-align:center">'
                         f'<input type="radio" name="{prefix}{key}" value="{val}"{checked}></td>')
        out.append(f"<tr><td>{esc(label)}</td>" + "".join(cells) + "</tr>")
    out.append("</table>")
    return "".join(out)


def parse_perms_form(f, prefix="perm_"):
    perms = {}
    for key, _ in MODULES:
        v = f.get(prefix + key)
        if v in (PERM_READ, PERM_WRITE):
            perms[key] = v
    return perms


def page_users(user, p):
    rows = auth.list_users(p.get("q", ""))
    names = dict(MODULES)
    data = []
    for r in rows:
        perms = effective_perms(r)
        tags = "、".join(f"{names[k]}({'可编辑' if v == PERM_WRITE else '只读'})"
                        for k, v in perms.items()) or "（无）"
        src = "自定义" if r.get("perms_dict") else "角色默认"
        link = ""
        if r["username"] != "admin" and can(user, "users", True):
            link = f' <a href="/users?edit={r["id"]}">分配权限</a>'
        data.append((esc(r["username"]) + link, esc(r.get("full_name") or "-"),
                     esc(ROLE_NAMES.get(r["role"], r["role"])), esc(src), esc(tags),
                     '<span class="ok">启用</span>' if r["active"] else '<span class="muted">停用</span>',
                     esc(r.get("last_login") or "-")))
    body = ""
    if not can(user, "users", True):
        return '<div class="msg">当前账号对用户模块只有只读权限</div>' + \
            table(["账号", "姓名", "角色", "权限来源", "可访问分区", "状态", "最近登录"],
                  data, ["", "", "", "c", "", "c", ""])

    eid = p.get("edit", "")
    if eid:
        row = auth.get_user(int(eid)) if str(eid).isdigit() else None
        if row and row["username"] == "admin":
            body += '<div class="msg">内置管理员固定拥有全部分区权限，不可修改</div>'
            row = None
        if row:
            perms = effective_perms(row)
            role_opts = "".join(
                f'<option value="{k}"{" selected" if k == row["role"] else ""}>{v}</option>'
                for k, v in ROLE_NAMES.items() if k != "admin")
            body += (
                f'<div class="panel"><h3>分配权限 · {esc(row["username"])}</h3>'
                f'<form method="post" action="/do/user_save">'
                f'<input type="hidden" name="id" value="{row["id"]}">'
                '<div class="toolbar">姓名：<input name="full_name" value="'
                + esc(row.get("full_name") or "") + '">'
                ' 角色：<select name="role">' + role_opts + '</select>'
                ' 重置密码：<input name="password" placeholder="留空不修改">'
                ' <label><input type="checkbox" name="active" value="1"'
                + (" checked" if row["active"] else "") + '>启用</label>'
                ' <button type="submit">保存</button></div>'
                '<p class="hint">全部选「不开放」= 取消单独分配，恢复为随角色自动；'
                '角色下拉仅作为权限模板，可再逐项覆盖。</p>'
                + perms_fields(perms) + '</form></div>')

    role_opts = "".join(f'<option value="{k}">{v}</option>'
                        for k, v in ROLE_NAMES.items() if k != "admin")
    body += ('<div class="panel"><h3>新增用户</h3><form method="post" action="/do/user_save">'
             '<div class="toolbar">'
             '<input name="username" placeholder="登录账号" required>'
             '<input name="full_name" placeholder="姓名">'
             '<select name="role">' + role_opts + '</select>'
             '<input name="password" placeholder="初始密码" value="123456">'
             '<button type="submit">保存</button></div>'
             '<p class="hint">下方矩阵全部留「不开放」= 随角色自动；'
             '也可先建账号，之后再点「分配权限」。</p>'
             + perms_fields() + '</form></div>')
    return body + table(["账号", "姓名", "角色", "权限来源", "可访问分区", "状态", "最近登录"],
                        data, ["", "", "", "c", "", "c", ""])


def page_logs(user, p):
    rows = auth.list_logs(p.get("q", ""), limit=300)
    data = [(esc(r["created_at"]), esc(r.get("username") or "-"), esc(r.get("module") or ""),
             esc(r.get("action") or ""), esc(r.get("ref_no") or "-"), esc(r.get("detail") or ""))
            for r in rows]
    return search_form("/logs", "操作人 / 动作 / 单号") + \
        table(["时间", "操作人", "模块", "动作", "相关单据", "详情"], data)


def page_import(user, p):
    """批量导入：粘贴 CSV 或选文件（由浏览器读入，无需 multipart 解析）"""
    kind = p.get("kind", "products")
    if kind not in importer.KIND_LABELS:
        kind = "products"

    opts = "".join(
        f'<option value="{k}"{" selected" if k == kind else ""}>{esc(v)}</option>'
        for k, v in importer.KIND_LABELS.items())
    tpl_links = " &nbsp;|&nbsp; ".join(
        f'<a href="data:text/csv;charset=utf-8,{urllib.parse.quote(importer.template_csv(k))}"'
        f' download="导入模板_{v}.csv">{v}模板</a>'
        for k, v in importer.KIND_LABELS.items())

    tips = {
        "products": "必填 SKU 与名称；分类必须已存在（可在命令行用 <code>ops.py addcat</code> 添加）。",
        "partners": "必填编码与名称；类型可填「客户 / 供应商 / 客户/供应商」，留空按下方默认。",
        "opening_stock": "SKU 必须已存在于商品资料；数量为「设定为」语义，重复导入不会叠加。",
    }
    return _io_bar() + f"""
<div class="panel">
  <h3 style="margin-bottom:8px">批量导入</h3>
  <div style="color:#6B7280;font-size:13px;margin-bottom:12px">
    支持 Excel 另存为的 CSV，<b>GBK 与 UTF-8 编码都会自动识别</b>；第一行须为表头，列名可用中文（如 SKU、名称、成本价）。
    <br>{tips[kind]}
  </div>
  <div style="font-size:13px;margin-bottom:10px">模板下载：{tpl_links}</div>
  <form method="post" action="/do/import_csv" id="impform">
    <div class="toolbar">
      <select name="kind" onchange="location.href='/import?kind='+this.value">
        {opts}
      </select>
      <input type="file" id="csvfile" accept=".csv,.txt" style="padding:6px">
      <span style="color:#6B7280;font-size:12.5px">选文件后内容会自动填入下方文本框，也可直接粘贴</span>
    </div>
    <div class="toolbar" id="ptypebar" style="display:{'block' if kind == 'partners' else 'none'}">
      默认类型：
      <select name="ptype">
        <option value="CUSTOMER">客户</option>
        <option value="SUPPLIER">供应商</option>
        <option value="BOTH">客户/供应商</option>
      </select>
    </div>
    <textarea name="csv_text" id="csvtext" rows="12" spellcheck="false"
      placeholder="SKU,名称,分类,成本价,销售价&#10;A-001,示例商品,成品,10,18"
      style="width:100%;padding:10px;border:1px solid #DFE4EC;border-radius:6px;
             font-family:Consolas,monospace;font-size:13px"></textarea>
    <div class="toolbar" style="margin-top:10px">
      <label><input type="checkbox" name="dry" value="1"> 只预校验不写入</label>
      <label><input type="checkbox" name="strict" value="1"> 严格模式（有一行错就整批不导）</label>
      <label><input type="checkbox" name="skipexist" value="1"> 已存在的跳过不更新</label>
      <button type="submit">开始导入</button>
    </div>
  </form>
  <div style="color:#6B7280;font-size:12.5px;margin-top:10px">
    建议先勾「只预校验」跑一遍，确认无误再正式导入。导入前记得先备份：<code>python ops.py backup</code>
  </div>
</div>
<script>
document.getElementById('csvfile').addEventListener('change', function(e){{
  var f = e.target.files[0]; if(!f) return;
  var box = document.getElementById('csvtext');
  // 先按 UTF-8 读；出现替换字符 U+FFFD 说明其实是 GBK（Excel 另存为的默认编码），重读一次
  var r1 = new FileReader();
  r1.onload = function(){{
    if (r1.result.indexOf('\\uFFFD') >= 0) {{
      var r2 = new FileReader();
      r2.onload = function(){{ box.value = r2.result; }};
      r2.readAsText(f, 'GBK');
    }} else {{ box.value = r1.result; }}
  }};
  r1.readAsText(f, 'UTF-8');
}});
document.getElementById('impform').addEventListener('submit', function(){{
  var t = document.getElementById('csvtext');
  if(!t.value.trim()){{ alert('请先选择 CSV 文件，或把内容粘贴到文本框'); return false; }}
}});
</script>"""


def page_password(user, p):
    """修改本人登录口令。v1.0：出厂/重置口令不再强制修改，用户可自行更改。"""
    tip = ('<div class="msg">当前账号使用的是系统出厂口令或被重置过的口令，'
           '出于安全建议尽快设置你自己的口令。</div>'
           if user.get("must_change") else "")
    return f"""{tip}<div class="panel" style="max-width:460px">
<form method="post" action="/do/change_pwd">
<div style="margin-bottom:10px"><input type="password" name="old_password"
 placeholder="当前口令" autocomplete="current-password" style="width:100%"></div>
<div style="margin-bottom:10px"><input type="password" name="new_password"
 placeholder="新口令（至少 6 位）" autocomplete="new-password" style="width:100%"></div>
<div style="margin-bottom:14px"><input type="password" name="new_password2"
 placeholder="确认新口令" autocomplete="new-password" style="width:100%"></div>
<button type="submit">修改口令</button></form></div>"""


_EXPORT_KINDS = {
    "products": "商品资料",
    "partners": "往来单位",
    "stock": "当前库存",
}

_PARTNER_CN = {"CUSTOMER": "客户", "SUPPLIER": "供应商", "BOTH": "客户/供应商"}


def _export_csv_text(kind: str) -> str:
    """生成导出 CSV 文本。

    表头直接复用导入模板（importer._TEMPLATES），保证「导出 → 再导入」闭环自洽，
    不会出现列名对不上导致整批导入失败。带 BOM，Excel 打开不乱码。
    """
    buf = io.StringIO()
    w = csv.writer(buf)
    conn = db.connect()
    try:
        if kind == "partners":
            w.writerow(importer._TEMPLATES["partners"][0])
            for r in db.rows_to_dicts(conn.execute(
                    "SELECT code, name, type, contact, phone, address,"
                    " payment_terms_days, credit_limit, remark"
                    " FROM partners WHERE active = 1 ORDER BY code").fetchall()):
                w.writerow([r["code"], r["name"],
                            _PARTNER_CN.get(r["type"], "客户"),
                            r["contact"], r["phone"], r["address"],
                            r["payment_terms_days"], r["credit_limit"],
                            r["remark"]])
        elif kind == "stock":
            w.writerow(importer._TEMPLATES["opening_stock"][0])
            for r in db.rows_to_dicts(conn.execute(
                    "SELECT p.sku AS sku, w.name AS wh, i.qty AS qty,"
                    " i.avg_cost AS cost"
                    " FROM inventory i"
                    " JOIN products p ON p.id = i.product_id"
                    " JOIN warehouses w ON w.id = i.warehouse_id"
                    " WHERE i.qty <> 0 ORDER BY p.sku").fetchall()):
                w.writerow([r["sku"], r["wh"], r["qty"], r["cost"]])
        else:
            w.writerow(importer._TEMPLATES["products"][0])
            for r in db.rows_to_dicts(conn.execute(
                    "SELECT p.sku AS sku, p.name AS name,"
                    " IFNULL(c.name, '') AS cat, p.spec AS spec, p.unit AS unit,"
                    " p.barcode AS barcode, p.cost_price AS cost,"
                    " p.sale_price AS sale, p.safety_stock AS safe,"
                    " p.reorder_point AS rop, p.lead_time_days AS lead,"
                    " p.abc_class AS abc, p.active AS active"
                    " FROM products p"
                    " LEFT JOIN categories c ON c.id = p.category_id"
                    " WHERE p.active = 1 ORDER BY p.sku").fetchall()):
                w.writerow([r["sku"], r["name"], r["cat"], r["spec"], r["unit"],
                            r["barcode"], r["cost"], r["sale"], r["safe"],
                            r["rop"], r["lead"], r["abc"],
                            "是" if r["active"] else "否"])
    finally:
        conn.close()
    return "\ufeff" + buf.getvalue()


def page_export(user, p):
    """数据导出：生成与导入模板同格式的 CSV，供复制或另存。

    刻意不走 response 附件下载——安卓 WebView 普遍不处理下载，
    改为把文本直接呈现在页面里，长按全选复制即可把数据带出手机。
    """
    kind = p.get("kind", "products")
    if kind not in _EXPORT_KINDS:
        kind = "products"
    text = _export_csv_text(kind) if p.get("gen") == "1" else ""
    cnt = max(0, text.count("\n") - 1) if text else 0
    opts = "".join(
        f'<option value="{k}"{" selected" if k == kind else ""}>{esc(v)}</option>'
        for k, v in _EXPORT_KINDS.items())
    return _io_bar() + f"""
<div class="panel">
  <h3 style="margin-bottom:8px">数据导出</h3>
  <form method="get" action="/export">
    <input type="hidden" name="gen" value="1">
    <div class="iobar">
      <select name="kind" onchange="this.form.submit()">{opts}</select>
      <button type="submit">生成 CSV</button>
    </div>
  </form>
  <div style="color:#6B7280;font-size:12.5px;margin:10px 0 8px">
    共 {cnt} 条。表头与导入模板一致，导出的内容可直接再导入本系统或电脑版。
  </div>
  <textarea id="csvout" readonly rows="14" spellcheck="false"
    style="width:100%;padding:10px;border:1px solid #DFE4EC;border-radius:6px;
           font-family:Consolas,monospace;font-size:13px"
    placeholder="点上面的「生成 CSV」后，内容会显示在这里">{esc(text)}</textarea>
  <div class="iobar" style="margin-top:10px">
    <button type="button" class="gray" onclick="(function(b){{var t=document.getElementById('csvout');t.select();try{{document.execCommand('copy');b.innerText='已复制'}}catch(e){{b.innerText='请长按全选复制'}}}})(this)">复制全部</button>
    <span>手机上若复制不成功，长按文本框用系统菜单全选复制</span>
  </div>
</div>"""


ROUTES = {
    "import": ("products", "Excel/CSV 批量导入商品、往来单位与期初库存", page_import, "批量导入"),
    "export": ("products", "导出 CSV：与导入模板同格式，可直接复制或另存", page_export, "数据导出"),
    "password": ("dashboard", "修改本人登录口令", page_password, "修改口令"),
    "dashboard": ("dashboard", "关键经营指标一屏掌握", page_dashboard),
    "products": ("products", "商品档案、价格、安全库存与 ABC 分类", page_products),
    "partners": ("partners", "客户与供应商档案、账期与信用额度", page_partners),
    "inventory": ("inventory", "实时库存、流水与缺货预警", page_inventory),
    "warehouse": ("warehouse", "库区实时分布、占用率与钻取明细", page_warehouse),
    "purchase": ("purchase", "采购订单：确认 → 收货入库 → 应付", page_purchase),
    "sales": ("sales", "销售订单：确认 → 出库发货 → 应收", page_sales),
    "returns": ("returns", "销售退货 / 采购退货：入库退货区或出库，自动红冲应收应付", page_returns, "退货管理"),
    "marketplace": ("marketplace", "对接 Shopify / TikTok Shop：授权、SKU 映射、拉单入库与回写库存", page_marketplace, "市场对接"),
    "finance": ("finance", "收付款、应收应付与账龄", page_finance),
    "reports": ("reports", "销售 / 采购 / 库存多维分析", page_reports),
    "users": ("users", "用户、角色权限", page_users),
    "logs": ("logs", "系统操作留痕", page_logs),
}


# ================================================================ 服务器
class Handler(BaseHTTPRequestHandler):
    server_version = "ERP/1.0"

    def log_message(self, fmt, *args):
        pass

    # ------------------------------------------------ 工具
    def _send(self, body, code=200, headers=None):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _send_bin(self, data: bytes, ctype: str, cache=True):
        """发送二进制资源（favicon 等）"""
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if cache:
            self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, to):
        self.send_response(303)
        self.send_header("Location", to)
        self.end_headers()

    def _cookies(self):
        raw = self.headers.get("Cookie", "")
        out = {}
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                out[k] = v
        return out

    def _user(self):
        tok = self._cookies().get("sid")
        return SESSIONS.get(tok)

    def _put_flash(self, tok, ok: bool, text):
        """写入一次性操作提示，下一次 GET 渲染后即销毁"""
        if tok:
            FLASH[tok] = ("ok" if ok else "err", text or "")

    def _take_flash(self, tok) -> str:
        """取出并销毁一次性操作提示，返回可直接嵌入页面的 HTML"""
        if not tok:
            return ""
        item = FLASH.pop(tok, None)
        if not item:
            return ""
        kind, text = item
        return f'<div class="flash {"ok" if kind == "ok" else "err"}">{esc(text)}</div>'

    def _safe_referer(self) -> str:
        """只接受本站相对路径作为回跳目标，防止开放重定向"""
        ref = self.headers.get("Referer") or "/dashboard"
        try:
            path = urllib.parse.urlparse(ref).path or "/dashboard"
        except Exception:
            return "/dashboard"
        if not path.startswith("/") or path.startswith("//"):
            return "/dashboard"
        return path

    def _params(self):
        u = urllib.parse.urlparse(self.path)
        return urllib.parse.parse_qs(u.query), {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}

    def _form(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        d = urllib.parse.parse_qs(raw)
        return {k: v[0] for k, v in d.items()}

    # ------------------------------------------------ GET
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        _, p = self._params()
        # v2.1.0：站点图标（浏览器无会话也会请求，须在鉴权前放行）
        if path in ("/favicon.ico", "/favicon.png", "/logo.png"):
            icon = _favicon_bytes()
            if icon:
                return self._send_bin(icon, "image/png")
            self.send_response(404)
            self.end_headers()
            return
        if path in ("/", "/login"):
            return self._send(login_page(""))
        if path == "/logout":
            tok = self._cookies().get("sid")
            SESSIONS.pop(tok, None)
            return self._redirect("/login")
        user = self._user()
        if not user:
            return self._redirect("/login")
        # v1.0：出厂/重置口令不再强制改密，登录即可进入业务功能
        # （如需修改口令，自行到顶部「修改口令」页操作）
        tok = self._cookies().get("sid")
        flash = self._take_flash(tok)
        if path == "/dashboard":
            return self._send(layout(user, "dashboard", "经营看板", "关键经营指标一屏掌握",
                                     page_dashboard(user, p), flash))
        if path == "/po":
            return self._send(layout(user, "purchase", "采购订单明细", "",
                                     _doc_detail("PURCHASE", user, p), flash))
        if path == "/so":
            return self._send(layout(user, "sales", "销售订单明细", "",
                                     _doc_detail("SALE", user, p), flash))
        if path == "/rt":
            return self._send(layout(user, "returns", "退货单明细", "",
                                     page_return_detail(user, p), flash))
        key = path.lstrip("/")
        if key in ROUTES:
            entry = ROUTES[key]
            module, desc, fn = entry[0], entry[1], entry[2]
            # 第 4 项可选：自定义页面标题（导入页这类不属于标准模块的页面会用到）
            title = entry[3] if len(entry) > 3 else dict(MODULES).get(module, module)
            if not can(user, module):
                return self._send(layout(user, "dashboard", "无权访问", "", '<div class="msg">当前角色无此模块权限</div>', flash))
            try:
                body = fn(user, p)
            except Exception as e:
                body = f'<div class="msg">页面出错：{esc(e)}</div>'
            return self._send(layout(user, module, title, desc, body, flash))
        return self._redirect("/dashboard")

    # ------------------------------------------------ POST
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/login":
            f = self._form()
            user = auth.authenticate(f.get("username", ""), f.get("password", ""))
            if not user:
                return self._send(login_page("用户名或密码错误"))
            tok = secrets.token_hex(16)
            SESSIONS[tok] = user
            self.send_response(303)
            self.send_header("Location", "/dashboard")
            # HttpOnly：JS 取不到会话令牌；SameSite=Lax：挡住最常见的跨站表单提交
            self.send_header("Set-Cookie", f"sid={tok}; Path=/; HttpOnly; SameSite=Lax")
            self.end_headers()
            return
        user = self._user()
        if not user:
            return self._redirect("/login")
        f = self._form()
        try:
            ok, msg = handle_action(user, path, f)
        except Exception as e:
            ok, msg = False, f"操作失败：{e}"
        self._put_flash(self._cookies().get("sid"), ok, msg)
        self.send_response(303)
        self.send_header("Location", self._safe_referer())
        self.end_headers()


def handle_action(user, path, f):
    """返回 (ok: bool, msg: str)。ok 决定页面提示条是绿色成功还是红色失败。"""
    act = path.replace("/do/", "")
    if act == "change_pwd":                       # v1.5.1：修改本人登录口令
        new = f.get("new_password", "")
        if new != f.get("new_password2", ""):
            return False, "两次输入的新口令不一致"
        ok, msg = auth.change_password(user["id"], f.get("old_password", ""), new)
        if ok:
            user["must_change"] = 0               # 同步会话，改完即可正常访问
        return ok, msg
    if act == "import_csv":
        kind = f.get("kind", "products")
        need = {"products": "products", "partners": "partners",
                "opening_stock": "inventory"}.get(kind)
        if not need or not can(user, need, True):
            return False, f"无权限导入{importer.KIND_LABELS.get(kind, kind)}"
        text = f.get("csv_text", "")
        if not text.strip():
            return False, "请先选择 CSV 文件，或把内容粘贴到文本框"
        ptype = f.get("ptype", "CUSTOMER").upper()
        if ptype not in ("CUSTOMER", "SUPPLIER", "BOTH"):
            ptype = "CUSTOMER"
        on_exists = "skip" if f.get("skipexist") else "update"
        dry = bool(f.get("dry"))
        label = importer.KIND_LABELS[kind]

        def _detail(res):
            d = "；".join(f"第{e['row']}行 {e['msg']}" for e in res["errors"][:3])
            more = f" 等共 {len(res['errors'])} 处" if len(res["errors"]) > 3 else ""
            return d + more

        try:
            if f.get("strict"):
                pre = importer.run_import(text, kind, ptype, on_exists,
                                          dry_run=True, user=user)
                if pre["errors"]:
                    return False, (f"严格模式：预校验未通过，整批未导入。"
                                   f"{_detail(pre)}")
            res = importer.run_import(text, kind, ptype, on_exists,
                                      dry_run=dry, user=user)
        except Exception as e:                        # noqa: BLE001
            return False, f"导入失败：{e}"

        tail = "（预校验，未写入）" if dry else ""
        msg = (f"{label}导入完成{tail}：新增 {res['created']}，更新 {res['updated']}，"
               f"跳过 {res['skipped']}，失败 {res['failed']}")
        if res["errors"]:
            msg += f"。问题：{_detail(res)}"
        return res["failed"] == 0, msg
    if act == "product_save":
        if not can(user, "products", True):
            return False, "无权限"
        return master.save_product(f)
    if act == "partner_save":
        if not can(user, "partners", True):
            return False, "无权限"
        return master.save_partner(f)
    if act == "doc_action":
        kind = f.get("kind")
        oid = int(f.get("id", 0))
        a = f.get("a")
        svc = purchase if kind == "PURCHASE" else sales
        if not can(user, "purchase" if kind == "PURCHASE" else "sales", True):
            return False, "无权限"
        if a == "confirm":
            ok, m = svc.confirm_order(oid, user, force=True)
        elif a == "cancel":
            ok, m = svc.cancel_order(oid, user)
        elif a == "exec":
            order = svc.get_order(oid)
            done_key = "received_qty" if kind == "PURCHASE" else "delivered_qty"
            payload = {i["id"]: float(i["qty"]) - float(i[done_key]) for i in order["items"]}
            payload = {k: v for k, v in payload.items() if v > 1e-6}
            fn = svc.receive_order if kind == "PURCHASE" else svc.deliver_order
            ok, m = fn(oid, payload, user)
        else:
            ok, m = False, "未知动作"
        return ok, m
    if act == "pay_add":
        if not can(user, "finance", True):
            return False, "无权限"
        return finance.add_payment(f, user)
    if act == "return_save":
        if not can(user, "returns", True):
            return False, "无权限"
        rt_type = f.get("rt_type", "SALE_RETURN")
        ref_key = f.get("ref_key", "")
        if ":" not in ref_key:
            return False, "请选择关联原单"
        kind, ref_id = ref_key.split(":", 1)
        ref_id = int(ref_id) if ref_id.isdigit() else 0
        ref_type = "SALE" if kind == "SO" else "PURCHASE"
        if ref_id <= 0:
            return False, "关联原单无效"
        # 自动按可退量生成明细（整单退回）
        lines = rt_svc.list_returnable_lines(rt_type, ref_id)
        items = [{"product_id": ln["product_id"], "ref_line_id": ln["ref_line_id"],
                  "qty": ln["returnable"], "unit_price": ln["unit_price"]}
                 for ln in lines if ln["returnable"] > 1e-6]
        if not items:
            return False, "该原单没有可退数量"
        header = {"rt_type": rt_type, "ref_id": ref_id, "order_date": today(),
                  "remark": f.get("remark", ""), "status": "DRAFT"}
        return rt_svc.save_order(header, items, user)
    if act == "return_action":
        if not can(user, "returns", True):
            return False, "无权限"
        rid = int(f.get("id", 0) or 0)
        a = f.get("a")
        if a == "confirm":
            return rt_svc.confirm_order(rid, user)
        if a == "post":
            return rt_svc.post_return(rid, user)
        if a == "cancel":
            return rt_svc.cancel_order(rid, user)
        return False, "未知动作"
    if act == "zone_transfer":
        if not can(user, "inventory", True):
            return False, "无权限"
        from .services import master as _m
        sku = (f.get("sku") or "").strip()
        prod = _m.get_product_by_sku(sku)
        if not prod:
            return False, f"未找到 SKU：{sku}"
        try:
            wid = int(f.get("wh"))
            fz = int(f.get("from_zone"))
            tz = int(f.get("to_zone"))
            qty = float(f.get("qty"))
        except (TypeError, ValueError):
            return False, "库区或数量填写有误"
        ok, m = zone_svc.transfer_zone(prod["id"], wid, fz, tz, qty, user)
        return ok, m
    if act == "user_save":
        if not can(user, "users", True):
            return False, "无权限"
        perms = parse_perms_form(f)
        uid = f.get("id", "")
        if uid and str(uid).isdigit():
            row = auth.get_user(int(uid))
            if not row:
                return False, "用户不存在"
            if row["username"] == "admin":
                return False, "内置管理员拥有全部权限，不可修改"
            ok, m = auth.update_user(int(uid), f.get("full_name", row["full_name"]),
                                     f.get("role", row["role"]),
                                     1 if f.get("active") else 0,
                                     f.get("password") or None, perms)
        else:
            ok, m = auth.create_user(f.get("username", ""), f.get("full_name", ""),
                                     f.get("role", "viewer"), f.get("password") or "123456",
                                     1, perms)
        return ok, m
    # ---------------- 市场对接 ----------------
    if act == "mp_cred_save":
        if not can(user, "marketplace", True):
            return False, "无权限"
        ch = f.get("channel", "")
        if ch not in MARKETPLACE["channels"]:
            return False, "未知渠道"
        fields = {k: f.get(k, "").strip() for k in
                  ("shop", "access_token", "refresh_token", "note")}
        fields["status"] = "ACTIVE" if f.get("status") == "ACTIVE" else "INACTIVE"
        return mp_svc.save_cred(ch, **fields)
    if act == "mp_map_save":
        if not can(user, "marketplace", True):
            return False, "无权限"
        pid = int(f.get("product_id", 0) or 0)
        if pid <= 0:
            return False, "请选择商品"
        psku = (f.get("platform_sku") or "").strip()
        if not psku:
            return False, "请填写平台SKU"
        active = 1 if f.get("active") else 0
        return mp_svc.save_sku_map(pid, f.get("channel", ""), psku,
                                   (f.get("logistics_code") or "").strip(), active)
    if act == "mp_map_del":
        if not can(user, "marketplace", True):
            return False, "无权限"
        return mp_svc.delete_sku_map(int(f.get("id", 0) or 0))
    if act == "mp_sync":
        if not can(user, "marketplace", True):
            return False, "无权限"
        return mp_svc.sync_all_active(user)
    if act == "mp_push":
        if not can(user, "marketplace", True):
            return False, "无权限"
        creds = [c for c in mp_svc.list_credentials() if c.get("status") == "ACTIVE"]
        if not creds:
            return True, "无已激活渠道，未回写"
        parts = []
        for c in creds:
            ok, m = mp_svc.push_inventory_all(c["channel"])
            parts.append(f"{c['channel']}: {m}")
        return True, "；".join(parts)
    return False, "未处理"


def login_page(err=""):
    err_html = f'<div class="msg">{esc(err)}</div>' if err else ""
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<link rel="icon" type="image/png" href="/favicon.ico">
<title>登录 · {APP_NAME}</title><style>{CSS}</style></head><body>
<header class="hdr"><div class="hdr-in">
<div class="brand"><img class="mark" src="/logo.png" alt="天宫">{APP_NAME}<span class="ver">v{APP_VERSION}</span></div>
</div></header>
<div class="login"><h2>{APP_NAME} 后台</h2><p>采购 · 销售 · 库存 · 资金 一体化管理</p>
{err_html}
<form method="post" action="/login">
<input name="username" placeholder="用户名" autocomplete="username" autofocus>
<input name="password" type="password" placeholder="密码" autocomplete="current-password">
<button type="submit">登 录</button></form>
<div class="foot">初始管理员账号：admin，初始口令见《系统说明.html》默认账号表<br>
演示账号：manager、sales、purchase、warehouse、finance</div>
</div></body></html>"""


def serve(port: int = 8765, open_browser: bool = True, host: str = "127.0.0.1"):
    """启动 Web 后台。

    host="127.0.0.1" 仅本机可访问（默认，安全）；
    host="0.0.0.0"   监听全部网卡，局域网内其他电脑可通过本机 IP 访问。
    """
    db.init_db()
    if host in ("0.0.0.0", "", "::"):
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            lan_ip = s.getsockname()[0]
            s.close()
        except Exception:
            lan_ip = "127.0.0.1"
        shown = lan_ip
    else:
        shown = host
    url = f"http://{shown}:{port}"
    print(f"[ERP] Web 后台已启动：{url}   (监听 {host}:{port})")
    if open_browser:
        try:
            webbrowser.open(f"http://127.0.0.1:{port}")
        except Exception:
            pass
    _start_marketplace_scheduler()
    ThreadingHTTPServer((host, port), Handler).serve_forever()


def _start_marketplace_scheduler():
    """后台定时同步（方案2）：按 config.MARKETPLACE.sync_interval_min 周期拉单。

    独立守护线程，每个渠道用独立短事务（db.txn），与请求线程互不持锁；
    间隔 <=0 时不启动，仅可手动触发。异常被捕获写入 sync_log，绝不拖垮 Web 服务。
    """
    interval = MARKETPLACE.get("sync_interval_min", 0)
    if not interval or interval <= 0:
        return
    import threading

    def _loop():
        sys_user = {"username": "定时同步", "role": "system"}
        while True:
            import time
            time.sleep(interval * 60)
            try:
                mp_svc.sync_all_active(sys_user)
            except Exception as e:  # 单个周期失败不能让线程死掉
                try:
                    mp_svc.log_sync("SYSTEM", "SCHEDULER", "", "FAIL",
                                    f"定时同步异常：{e}")
                except Exception:
                    pass

    t = threading.Thread(target=_loop, name="mp-scheduler", daemon=True)
    t.start()
    print(f"[ERP] 市场对接定时同步已启动：每 {interval} 分钟")
