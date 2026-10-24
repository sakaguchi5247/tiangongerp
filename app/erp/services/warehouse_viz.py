# -*- coding: utf-8 -*-
"""仓库可视化（v1.6.0）——纯标准库生成 SVG，零第三方依赖

输出可直接嵌入 Web 后台（http.server 返回的标准 HTML）的矢量仓库平面图：
- 库区色块按「占用率」做绿→黄→红色阶（与业界 WMS 一致：绿=空闲/黄=部分/红=满载）
- 每个库区显示实时件数、SKU 数、库存金额、占用率进度条
- 顶部 KPI 与「对账一致」徽标，证明分区数字 ≡ 进销存台账
- 库区可点击下钻（<a href>）查看该区 SKU 明细
桌面端 canvas 复用同一份 zone_summary 数据自行绘制，保证两端口径一致。
"""
from .. import db
from ..config import COMPANY_NAME, APP_NAME, APP_VERSION
from ..utils import today, money, num


# ---------------------------------------------------------------- 颜色
def _lerp(a, b, t):
    return int(a + (b - a) * t)


def _hex(r, g, b):
    return f"#{r:02X}{g:02X}{b:02X}"


def occupancy_color(ratio):
    """占用率→颜色：空/不限=灰蓝；低=绿；中=黄；高/超=红"""
    if ratio is None or ratio <= 0:
        return "#E2E8F0"
    if ratio >= 1.0:
        return "#DC2626"
    if ratio <= 0.6:
        # 绿 #16A34A → 黄 #EAB308
        t = ratio / 0.6
        return _hex(_lerp(0x16, 0xEA, t), _lerp(0xA3, 0xB3, t), _lerp(0x4A, 0x08, t))
    if ratio <= 0.9:
        # 黄 #EAB308 → 橙 #F97316
        t = (ratio - 0.6) / 0.3
        return _hex(_lerp(0xEA, 0xF9, t), _lerp(0xB3, 0x73, t), _lerp(0x08, 0x16, t))
    # 橙 → 红
    t = (ratio - 0.9) / 0.1
    return _hex(_lerp(0xF9, 0xDC, t), _lerp(0x73, 0x26, t), _lerp(0x16, 0x26, t))


# ---------------------------------------------------------------- 平面图
def build_floorplan_svg(warehouse_id: int, conn=None) -> str:
    own = conn is None
    conn = conn or db.connect()
    try:
        wh = conn.execute("SELECT * FROM warehouses WHERE id=?", (warehouse_id,)).fetchone()
        if not wh:
            return _empty_svg("未找到该仓库")
        wh_name = wh["name"]
        zones = _zone_rows(conn, warehouse_id)
        tot = _totals(conn, warehouse_id)
    finally:
        if own:
            conn.close()

    W, H = 960, 600
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
             f'font-family="\'Microsoft YaHei UI\',\'PingFang SC\',sans-serif" width="100%">']
    # 背景
    parts.append(f'<rect x="0" y="0" width="{W}" height="{H}" fill="#F4F6FA"/>')
    # 标题条
    badge = ('<rect x="700" y="20" width="240" height="34" rx="8" fill="#ECFDF5" stroke="#A7F3D0"/>'
             '<text x="712" y="42" font-size="15" fill="#065F46">✓ 分区对账一致</text>') \
        if tot["match"] else \
        ('<rect x="700" y="20" width="240" height="34" rx="8" fill="#FEF2F2" stroke="#FECACA"/>'
         f'<text x="712" y="42" font-size="15" fill="#991B1B">✗ 对账差异 {num(tot["diff"])}</text>')
    parts.append(
        f'<text x="24" y="42" font-size="20" font-weight="bold" fill="#1F2937">'
        f'{_esc(wh_name)} · 仓库实时分布</text>'
        f'<text x="24" y="62" font-size="12" fill="#6B7280">'
        f'{_esc(COMPANY_NAME)} · {APP_NAME} v{APP_VERSION} · {today()}</text>'
        + badge)
    # KPI：以库区合计为权威口径（Σ分区库存），与下方色块数字一致；
    # 若与仓库级 inventory 存在差异，KPI 显示为「库区合计」并提示对账异常。
    zone_qty = sum(float(z["qty"]) for z in zones)
    zone_val = sum(float(z["value"]) for z in zones)
    kpis = [
        ("总库存件数", num(zone_qty)),
        ("库存金额", money(zone_val)),
        ("库区数量", str(len(zones))),
        ("分区合计", num(tot["zone_qty"])),
    ]
    kx = 24
    for label, val in kpis:
        parts.append(f'<rect x="{kx}" y="78" width="216" height="58" rx="8" fill="#fff" stroke="#DFE4EC"/>')
        parts.append(f'<text x="{kx+14}" y="100" font-size="12" fill="#6B7280">{_esc(label)}</text>')
        parts.append(f'<text x="{kx+14}" y="124" font-size="18" font-weight="bold" fill="#1D4ED8">{_esc(val)}</text>')
        kx += 228

    # 仓库外框 + 库区网格
    fx, fy, fw, fh = 24, 154, 912, 410
    parts.append(f'<rect x="{fx}" y="{fy}" width="{fw}" height="{fh}" rx="14" fill="#fff" '
                 f'stroke="#CBD5E1" stroke-width="2"/>')
    parts.append(f'<text x="{fx+16}" y="{fy+24}" font-size="13" fill="#94A3B8">仓库平面 · 颜色=占用率</text>')
    n = max(len(zones), 1)
    cols = 3 if n > 3 else n
    rows = (n + cols - 1) // cols
    pad = 18
    gx0, gy0 = fx + pad, fy + 40
    gw = (fw - pad * (cols + 1)) / cols
    gh = (fh - 40 - pad * (rows + 1)) / rows
    for i, z in enumerate(zones):
        r, c = divmod(i, cols)
        x = gx0 + c * (gw + pad)
        y = gy0 + r * (gh + pad)
        parts.append(_zone_block(x, y, gw, gh, z))
    # 图例
    ly = fy + fh - 26
    parts.append(f'<text x="{gx0}" y="{ly+4}" font-size="12" fill="#64748B">占用率：</text>')
    leg = [("空闲", "#16A34A"), ("适中", "#EAB308"), ("偏高", "#F97316"), ("满载/超限", "#DC2626")]
    lx = gx0 + 56
    for name, col in leg:
        parts.append(f'<rect x="{lx}" y="{ly-10}" width="14" height="14" rx="3" fill="{col}"/>')
        parts.append(f'<text x="{lx+18}" y="{ly+2}" font-size="12" fill="#64748B">{name}</text>')
        lx += 96
    parts.append('</svg>')
    return "".join(parts)


def _zone_rows(conn, warehouse_id):
    from . import zones as zs
    return zs.zone_summary(warehouse_id)


def _totals(conn, warehouse_id):
    from . import zones as zs
    return zs.warehouse_totals(warehouse_id)


def _zone_block(x, y, w, h, z) -> str:
    name = z["name"]
    ztype = z["type_label"]
    qty = float(z["qty"])
    skus = int(z["skus"])
    value = float(z["value"])
    occ = z["occupancy"]
    color = z["color"] or occupancy_color(occ)
    fill = "#F8FAFD" if occ is None else occupancy_color(occ)
    # 卡片底色随占用率变化（淡色），标题条用类型色
    bar = min((occ or 0), 1.0)
    bar_w = max(int((w - 32) * bar), 0)
    occ_txt = "不限容量" if occ is None else f"{occ*100:.0f}%"
    cap_txt = "容量不限" if float(z["capacity"] or 0) == 0 else f"容量 {num(z['capacity'])} 件"
    zone_id = int(z["id"])
    # 点击下钻：库区链接到该区明细
    s = [f'<a href="/warehouse?zone={zone_id}">'
         f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{fill}" stroke="{color}" stroke-width="2" style="cursor:pointer"/>'
         f'<rect x="{x}" y="{y}" width="{w}" height="26" rx="10" fill="{color}" opacity="0.92"/>'
         f'<text x="{x+12}" y="{y+18}" font-size="14" font-weight="bold" fill="#fff">{_esc(name)}</text>'
         f'<text x="{x+w-12}" y="{y+18}" font-size="12" fill="#fff" text-anchor="end">{_esc(ztype)}</text>'
         f'<text x="{x+12}" y="{y+62}" font-size="13" fill="#475569">实时库存</text>'
         f'<text x="{x+12}" y="{y+92}" font-size="26" font-weight="bold" fill="#0F172A">{_esc(num(qty))} 件</text>'
         f'<text x="{x+12}" y="{y+118}" font-size="12" fill="#64748B">SKU {skus} 个 · {_esc(money(value))}</text>'
         f'<text x="{x+12}" y="{y+h-30}" font-size="12" fill="#64748B">占用率 {occ_txt} · {cap_txt}</text>'
         f'<rect x="{x+12}" y="{y+h-22}" width="{w-24}" height="10" rx="5" fill="#E5EAF3"/>'
         f'<rect x="{x+12}" y="{y+h-22}" width="{bar_w}" height="10" rx="5" fill="{color}"/>'
         f'<text x="{x+w-12}" y="{y+14}" font-size="11" fill="#fff" text-anchor="end"></text>'
         f'</a>']
    return "".join(s)


def _empty_svg(msg):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 200" width="100%">'
            f'<rect width="960" height="200" fill="#F4F6FA"/>'
            f'<text x="24" y="110" font-size="16" fill="#94A3B8">{_esc(msg)}</text></svg>')


def _esc(v):
    from xml.sax.saxutils import escape
    return escape(str(v if v is not None else ""))


# ---------------------------------------------------------------- 钻取明细
def zone_detail_svg(zone_id: int) -> str:
    from . import zones as zs
    zone = zs.get_zone(zone_id)
    if not zone:
        return _empty_svg("未找到该库区")
    rows = zs.zone_detail(zone_id)
    W, H = 960, max(220, 60 + len(rows) * 26)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="100%" '
             f'font-family="\'Microsoft YaHei UI\',sans-serif">']
    parts.append(f'<text x="24" y="34" font-size="18" font-weight="bold" fill="#1F2937">'
                 f'{_esc(zone["name"])} · SKU 明细（共 {len(rows)} 项）</text>')
    if not rows:
        parts.append(f'<text x="24" y="80" font-size="14" fill="#94A3B8">该库区当前无库存</text>')
        parts.append('</svg>')
        return "".join(parts)
    # 表头
    y = 60
    cols = [("SKU", 24, 140), ("商品", 170, 360), ("规格", 536, 300), ("数量", 700, 90), ("库存金额", 800, 130)]
    for label, cx, _ in cols:
        parts.append(f'<text x="{cx}" y="{y}" font-size="12" fill="#94A3B8">{label}</text>')
    parts.append(f'<line x1="20" y1="{y+8}" x2="940" y2="{y+8}" stroke="#E5EAF3"/>')
    maxv = max(float(r["value"]) for r in rows) or 1
    yy = y + 30
    for r in rows:
        parts.append(f'<text x="24" y="{yy}" font-size="13" fill="#0F172A">{_esc(r["sku"])}</text>')
        parts.append(f'<text x="170" y="{yy}" font-size="13" fill="#334155">{_esc(r["name"])}</text>')
        parts.append(f'<text x="536" y="{yy}" font-size="12" fill="#64748B">{_esc(r["spec"])}</text>')
        parts.append(f'<text x="700" y="{yy}" font-size="13" fill="#0F172A" text-anchor="end">{_esc(num(r["qty"]))} {_esc(r["unit"])}</text>')
        # 金额条
        bw = int((float(r["value"]) / maxv) * 110)
        parts.append(f'<rect x="800" y="{yy-11}" width="{bw}" height="13" rx="3" fill="#2563EB" opacity="0.85"/>')
        parts.append(f'<text x="800" y="{yy}" font-size="12" fill="#1E3A8A" dx="{bw+6}">{_esc(money(r["value"]))}</text>')
        yy += 26
    parts.append('</svg>')
    return "".join(parts)
