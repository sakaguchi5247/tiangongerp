# -*- coding: utf-8 -*-
"""批量导入（CSV）。

设计要点：
1. 零第三方依赖——只用标准库 csv，符合本项目"零依赖"的定位。
2. 编码自动识别：Windows 版 Excel 另存为 CSV 默认是 GBK，直接按 UTF-8 读会整片乱码。
   识别顺序 utf-8-sig → utf-8 → gbk，覆盖绝大多数来源。
3. 表头中英文通吃：用户可以直接用 Excel 里的中文列名，不必改成内部字段名。
4. 先预校验（dry_run）再落库：strict 模式下有任何一行不合规就整批不导入，
   避免"导了一半才发现格式错了，库里已经脏了"。
5. 逐行给出失败原因（第几行、哪个字段、为什么），而不是笼统报一句"导入失败"。
"""
import csv
import io
from pathlib import Path

from .. import db, config
from . import master, inventory


# ---------------------------------------------------------------- 表头映射
# 内部字段 -> 可识别的列名（小写比较，忽略空格与下划线）
_HEADERS = {
    "products": {
        "sku": ["sku", "编码", "商品编码", "货号", "code", "商品编号"],
        "name": ["名称", "商品名称", "品名", "name"],
        "category": ["分类", "类别", "商品分类", "category"],
        "spec": ["规格", "型号", "规格型号", "spec"],
        "unit": ["单位", "计量单位", "unit"],
        "barcode": ["条码", "条形码", "barcode"],
        "cost_price": ["成本价", "成本", "采购价", "进价", "cost_price", "cost"],
        "sale_price": ["销售价", "售价", "单价", "零售价", "sale_price", "price"],
        "safety_stock": ["安全库存", "最低库存", "safety_stock"],
        "reorder_point": ["再订货点", "订货点", "rop", "reorder_point"],
        "lead_time_days": ["采购提前期", "提前期", "lead_time_days", "leadtime"],
        "abc_class": ["abc分类", "abc", "abc_class"],
        "active": ["启用", "状态", "是否启用", "active"],
    },
    "partners": {
        "code": ["编码", "客户编码", "供应商编码", "单位编码", "code", "编号"],
        "name": ["名称", "客户名称", "供应商名称", "单位名称", "name"],
        "ptype": ["类型", "单位类型", "type", "ptype"],
        "contact": ["联系人", "contact"],
        "phone": ["电话", "手机", "联系电话", "phone", "tel"],
        "email": ["邮箱", "电子邮件", "email"],
        "address": ["地址", "联系地址", "address"],
        "tax_no": ["税号", "纳税人识别号", "tax_no"],
        "payment_terms_days": ["账期", "账期天数", "付款账期", "payment_terms_days"],
        "credit_limit": ["信用额度", "额度", "credit_limit"],
        "remark": ["备注", "remark"],
    },
    "opening_stock": {
        "sku": ["sku", "编码", "商品编码", "货号", "code"],
        "warehouse": ["仓库", "仓库名称", "warehouse"],
        "qty": ["数量", "期初数量", "库存数量", "qty", "quantity"],
        "cost": ["单位成本", "成本", "成本价", "单价", "cost", "unit_cost"],
    },
}

# 模板列顺序与示例值，供用户下载后照着填
_TEMPLATES = {
    # v1.5.1：补上「再订货点」列。过去导入不带该字段，save_product 会把它写成 0，
    # 而导出 CSV 也不含该列 —— 导出再导入会持续抹掉 ROP（实测 123 → 0）。
    "products": (["SKU*", "名称*", "分类", "规格", "单位", "条码",
                  "成本价", "销售价", "安全库存", "再订货点", "采购提前期", "ABC分类", "启用"],
                 ["DEMO-001", "示例商品", "成品", "500ml", "件", "6900000000001",
                  "10", "18", "20", "30", "7", "A", "是"]),
    "partners": (["编码*", "名称*", "类型", "联系人", "电话", "地址",
                  "账期天数", "信用额度", "备注"],
                 ["C-001", "示例客户", "客户", "张经理", "13800000000", "上海市",
                  "30", "50000", "新客户"]),
    "opening_stock": (["SKU*", "仓库*", "数量*", "单位成本"],
                      ["DEMO-001", "主仓库", "100", "10"]),
}

KIND_LABELS = {
    "products": "商品资料",
    "partners": "往来单位",
    "opening_stock": "期初库存",
}


class ImportError_(Exception):
    pass


# ---------------------------------------------------------------- 基础工具
def _read_text(src) -> tuple:
    """读取 CSV 文本，自动识别编码。src 可以是路径或文本内容。"""
    # 传进来的可能直接就是 CSV 文本（Web 端粘贴）。先判定是文本还是路径，
    # 注意 Windows 下对超长/含换行的字符串调 exists() 会抛异常，必须兜住。
    if isinstance(src, str):
        if "\n" in src or len(src) > 400:
            return src, "inline"
        try:
            if not Path(src).exists():
                return src, "inline"
        except (OSError, ValueError):
            return src, "inline"
    path = Path(src)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在：{path}")
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "unknown"


def _norm(s) -> str:
    # 去掉空格/下划线/星号：模板里用「SKU*」标记必填，用户手写也可能带
    return (str(s or "").strip().lower()
            .replace(" ", "").replace("_", "").replace("*", ""))


def _map_headers(headers, kind) -> dict:
    """把用户表头映射到内部字段，返回 {内部字段: 列索引}"""
    table = _HEADERS[kind]
    norm_map = {}
    for field, names in table.items():
        for n in names:
            norm_map[_norm(n)] = field
    out = {}
    for idx, h in enumerate(headers):
        f = norm_map.get(_norm(h))
        if f and f not in out:
            out[f] = idx
    return out


def _cell(row, idx):
    if idx is None or idx >= len(row):
        return ""
    return str(row[idx] or "").strip()


def _num(text, field, default=0.0):
    t = str(text or "").strip().replace(",", "").replace("¥", "").replace("￥", "")
    if t == "":
        return default
    try:
        return float(t)
    except ValueError:
        raise ImportError_(f"{field}「{text}」不是有效数字")


def _int(text, field, default=0):
    return int(_num(text, field, default))


def _bool(text, default=True):
    t = str(text or "").strip().lower()
    if t in ("", "是", "y", "yes", "1", "true", "启用", "有效", "on"):
        return 1
    if t in ("否", "n", "no", "0", "false", "停用", "无效", "off"):
        return 0
    return 1 if default else 0


def parse_csv(src, kind: str) -> tuple:
    """解析 CSV，返回 (行列表, 编码)。每行是 {内部字段: 值} 的字典列表。"""
    text, enc = _read_text(src)
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader]
    while rows and not any(c.strip() for c in rows[0]):
        rows.pop(0)                      # 跳过文件开头的空行
    if len(rows) < 2:
        raise ImportError_("文件内容为空或只有表头")

    colmap = _map_headers(rows[0], kind)
    required = "sku" if kind != "partners" else "code"
    if required not in colmap:
        raise ImportError_(
            f"没找到必填列「{required}」。请确认第一行是表头，"
            f"或先用 `ops.py template {kind}` 下载模板照着填。")

    out = []
    for r in rows[1:]:
        if not any(c.strip() for c in r):
            continue                     # 跳过空行
        out.append({f: _cell(r, i) for f, i in colmap.items()})
    return out, enc


# ---------------------------------------------------------------- 导入实现
def _resolve_category(conn, name: str):
    name = (name or "").strip()
    if not name:
        return None, None
    r = conn.execute("SELECT id FROM categories WHERE name=?", (name,)).fetchone()
    if r:
        return r["id"], None
    r = conn.execute("SELECT id FROM categories WHERE code=?", (name,)).fetchone()
    if r:
        return r["id"], None
    allc = [f"{x['name']}" for x in conn.execute("SELECT name FROM categories").fetchall()]
    return None, f"分类「{name}」不存在，现有分类：{'、'.join(allc) or '（无）'}"


def _resolve_warehouse(conn, name: str):
    name = (name or "").strip()
    if not name:
        return None, None
    r = conn.execute("SELECT id FROM warehouses WHERE name=?", (name,)).fetchone()
    if r:
        return r["id"], None
    r = conn.execute("SELECT id FROM warehouses WHERE code=?", (name,)).fetchone()
    if r:
        return r["id"], None
    allw = [x["name"] for x in conn.execute("SELECT name FROM warehouses").fetchall()]
    return None, f"仓库「{name}」不存在，现有仓库：{'、'.join(allw) or '（无）'}"


def _row_error(row_no, key, msg):
    return {"row": row_no, "key": key, "msg": msg}


def import_products(rows, on_exists="update", dry_run=False, user=None) -> dict:
    """导入商品。on_exists: update=已存在则更新 / skip=跳过。"""
    res = {"kind": "products", "total": len(rows), "created": 0, "updated": 0,
           "skipped": 0, "errors": []}
    seen = set()
    for i, r in enumerate(rows, start=2):          # 第 1 行是表头，数据从第 2 行起
        try:
            sku = (r.get("sku") or "").strip()
            name = (r.get("name") or "").strip()
            if not sku:
                raise ImportError_("SKU 为空")
            if not name:
                raise ImportError_("名称为空")
            if sku in seen:
                raise ImportError_(f"文件内 SKU「{sku}」重复")
            seen.add(sku)

            with db.txn() as conn:
                cid, err = _resolve_category(conn, r.get("category"))
                if err:
                    raise ImportError_(err)
                exist = conn.execute(
                    "SELECT id, reorder_point FROM products WHERE sku=?", (sku,)).fetchone()
                rop_raw = (r.get("reorder_point") or "").strip()
                if rop_raw:
                    rop = _num(rop_raw, "再订货点")
                else:
                    # CSV 没给就保留原值，绝不静默清零
                    rop = float(exist["reorder_point"]) if exist else 0.0
                abc = (r.get("abc_class") or "C").strip().upper()[:1] or "C"
                if abc not in ("A", "B", "C"):
                    abc = "C"
                payload = {
                    "sku": sku, "name": name, "category_id": cid,
                    "spec": r.get("spec", ""), "unit": r.get("unit") or "件",
                    "barcode": r.get("barcode", ""),
                    "cost_price": _num(r.get("cost_price"), "成本价"),
                    "sale_price": _num(r.get("sale_price"), "销售价"),
                    "safety_stock": _num(r.get("safety_stock"), "安全库存"),
                    "reorder_point": rop,
                    "lead_time_days": _int(r.get("lead_time_days"), "采购提前期", 7),
                    "abc_class": abc,
                    "active": _bool(r.get("active"), True),
                }
                if exist:
                    if on_exists == "skip":
                        res["skipped"] += 1
                        continue
                    payload["id"] = exist["id"]
                    if not dry_run:
                        ok, msg = master.save_product(payload)
                        if not ok:
                            raise ImportError_(msg)
                    res["updated"] += 1
                else:
                    if not dry_run:
                        ok, msg = master.save_product(payload)
                        if not ok:
                            raise ImportError_(msg)
                    res["created"] += 1
        except ImportError_ as e:
            res["errors"].append(_row_error(i, (r.get("sku") or "").strip(), str(e)))
        except Exception as e:                       # noqa: BLE001
            res["errors"].append(_row_error(i, (r.get("sku") or "").strip(), f"写入失败：{e}"))

    if not dry_run:
        _log(user, "批量导入商品", "商品",
             f"新增 {res['created']}，更新 {res['updated']}，失败 {len(res['errors'])}")
    return res


def import_partners(rows, ptype="CUSTOMER", on_exists="update",
                    dry_run=False, user=None) -> dict:
    """导入往来单位。ptype 为缺省类型，行内「类型」列可覆盖。"""
    res = {"kind": "partners", "total": len(rows), "created": 0, "updated": 0,
           "skipped": 0, "errors": []}
    seen = set()
    for i, r in enumerate(rows, start=2):
        try:
            code = (r.get("code") or "").strip()
            name = (r.get("name") or "").strip()
            if not code:
                raise ImportError_("编码为空")
            if not name:
                raise ImportError_("名称为空")
            if code in seen:
                raise ImportError_(f"文件内编码「{code}」重复")
            seen.add(code)

            t = (r.get("ptype") or "").strip()
            mapping = {"客户": "CUSTOMER", "供应商": "SUPPLIER", "客户/供应商": "BOTH",
                       "both": "BOTH", "customer": "CUSTOMER", "supplier": "SUPPLIER"}
            resolved = mapping.get(t.lower(), None) or \
                (t.upper() if t.upper() in ("CUSTOMER", "SUPPLIER", "BOTH") else ptype)

            with db.txn() as conn:
                exist = conn.execute(
                    "SELECT id FROM partners WHERE code=?", (code,)).fetchone()
                payload = {
                    "code": code, "name": name, "type": resolved,
                    "contact": r.get("contact", ""), "phone": r.get("phone", ""),
                    "email": r.get("email", ""), "address": r.get("address", ""),
                    "tax_no": r.get("tax_no", ""),
                    "payment_terms_days": _int(r.get("payment_terms_days"), "账期天数", 30),
                    "credit_limit": _num(r.get("credit_limit"), "信用额度"),
                    "remark": r.get("remark", ""),
                }
                if exist:
                    if on_exists == "skip":
                        res["skipped"] += 1
                        continue
                    payload["id"] = exist["id"]
                    if not dry_run:
                        ok, msg = master.save_partner(payload)
                        if not ok:
                            raise ImportError_(msg)
                    res["updated"] += 1
                else:
                    if not dry_run:
                        ok, msg = master.save_partner(payload)
                        if not ok:
                            raise ImportError_(msg)
                    res["created"] += 1
        except ImportError_ as e:
            res["errors"].append(_row_error(i, (r.get("code") or "").strip(), str(e)))
        except Exception as e:                       # noqa: BLE001
            res["errors"].append(_row_error(i, (r.get("code") or "").strip(), f"写入失败：{e}"))

    if not dry_run:
        _log(user, "批量导入往来单位", "往来单位",
             f"新增 {res['created']}，更新 {res['updated']}，失败 {len(res['errors'])}")
    return res


def import_opening_stock(rows, dry_run=False, user=None) -> dict:
    """导入期初库存。用「设定为」语义（重复导入结果一致，不会叠加）。"""
    res = {"kind": "opening_stock", "total": len(rows), "created": 0, "updated": 0,
           "skipped": 0, "errors": []}
    for i, r in enumerate(rows, start=2):
        try:
            sku = (r.get("sku") or "").strip()
            if not sku:
                raise ImportError_("SKU 为空")
            qty = _num(r.get("qty"), "数量")
            if qty < 0:
                raise ImportError_("数量不能为负")
            cost = _num(r.get("cost"), "单位成本")
            if cost < 0:
                raise ImportError_("单位成本不能为负")

            with db.txn() as conn:
                p = conn.execute(
                    "SELECT id,name FROM products WHERE sku=?", (sku,)).fetchone()
                if not p:
                    raise ImportError_(f"SKU「{sku}」在商品资料中不存在，请先导入商品")
                wid, err = _resolve_warehouse(conn, r.get("warehouse"))
                if err:
                    raise ImportError_(err)
                if wid is None:
                    wid = conn.execute(
                        "SELECT id FROM warehouses WHERE active=1 ORDER BY id LIMIT 1"
                    ).fetchone()["id"]
                if not dry_run:
                    # v1.5.1（P0-2）：必须把导入成本透传给 adjust_stock。
                    # 过去不传 unit_cost，流水按（新商品为 0 的）参考成本记账 →
                    # stock_moves.amount = 0，随后又直接 UPDATE avg_cost，
                    # 于是「库存余额 750、流水金额 0」，账账不符且现有自检查不出来。
                    try:
                        inventory.adjust_stock(
                            p["id"], wid, qty,
                            f"期初库存导入（单位成本 {cost:g}）", user,
                            unit_cost=cost if cost > 0 else None)
                    except db.OpAbort as e:
                        raise ImportError_(str(e))
                res["created"] += 1
        except ImportError_ as e:
            res["errors"].append(_row_error(i, (r.get("sku") or "").strip(), str(e)))
        except Exception as e:                       # noqa: BLE001
            res["errors"].append(_row_error(i, (r.get("sku") or "").strip(), f"写入失败：{e}"))

    if not dry_run:
        _log(user, "批量导入期初库存", "库存",
             f"导入 {res['created']} 行，失败 {len(res['errors'])}")
    return res


def _log(user, action, module, detail):
    try:
        from .auth import log
        with db.txn() as conn:
            log(conn, (user or {}).get("id"), (user or {}).get("username", ""),
                action, module, "", detail)
    except Exception:                                # noqa: BLE001
        pass


# ---------------------------------------------------------------- 统一入口
def run_import(src, kind: str, ptype="CUSTOMER", on_exists="update",
               dry_run=False, user=None) -> dict:
    """统一的导入入口。返回带 errors 明细的结果字典。"""
    if kind not in KIND_LABELS:
        raise ImportError_(f"不支持的导入类型：{kind}")
    rows, enc = parse_csv(src, kind)
    if kind == "products":
        res = import_products(rows, on_exists, dry_run, user)
    elif kind == "partners":
        res = import_partners(rows, ptype, on_exists, dry_run, user)
    else:
        res = import_opening_stock(rows, dry_run, user)
    res["encoding"] = enc
    res["label"] = KIND_LABELS[kind]
    res["dry_run"] = dry_run
    res["ok"] = res["created"] + res["updated"]
    res["failed"] = len(res["errors"])
    return res


def template_csv(kind: str) -> str:
    """生成 CSV 模板文本（带 BOM，Excel 打开不乱码）"""
    cols, sample = _TEMPLATES[kind]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    w.writerow(sample)
    return "﻿" + buf.getvalue()


def _write_text(path: Path, text: str) -> Path:
    """按二进制无换行转换写出。

    Windows 下用文本模式写 csv 会把 \\r\\n 二次转成 \\r\\r\\n，
    Excel/记事本打开就会多出空行，故必须 newline=""。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return path


def write_template(kind: str, directory=None) -> Path:
    path = Path(directory or config.EXPORT_DIR) / f"导入模板_{KIND_LABELS[kind]}.csv"
    return _write_text(path, template_csv(kind))


# ---------------------------------------------------------------- 导出
def export_csv(kind: str, directory=None) -> Path:
    """把现有数据导出成 CSV（带 BOM，Excel 双击不乱码）"""
    directory = Path(directory or config.EXPORT_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"导出_{KIND_LABELS[kind]}.csv"
    buf = io.StringIO()
    w = csv.writer(buf)
    with db.txn() as conn:
        if kind == "products":
            # v1.5.1：补上「再订货点」列，保证导出→再导入不丢字段
            w.writerow(["SKU", "名称", "分类", "规格", "单位", "条码",
                        "成本价", "销售价", "安全库存", "再订货点", "采购提前期",
                        "ABC分类", "启用"])
            for r in conn.execute(
                "SELECT p.sku,p.name,c.name cat,p.spec,p.unit,p.barcode,p.cost_price,"
                " p.sale_price,p.safety_stock,p.reorder_point,p.lead_time_days,p.abc_class,p.active"
                " FROM products p LEFT JOIN categories c ON c.id=p.category_id"
                " ORDER BY p.sku"):
                w.writerow([r["sku"], r["name"], r["cat"] or "", r["spec"], r["unit"],
                            r["barcode"], r["cost_price"], r["sale_price"],
                            r["safety_stock"], r["reorder_point"], r["lead_time_days"],
                            r["abc_class"], "是" if r["active"] else "否"])
        elif kind == "partners":
            # v1.5.1：补上「备注」列（partners 表同期新增 remark 字段）
            w.writerow(["编码", "名称", "类型", "联系人", "电话", "邮箱",
                        "地址", "税号", "账期天数", "信用额度", "备注"])
            for r in conn.execute("SELECT * FROM partners ORDER BY code"):
                tmap = {"CUSTOMER": "客户", "SUPPLIER": "供应商", "BOTH": "客户/供应商"}
                w.writerow([r["code"], r["name"], tmap.get(r["type"], r["type"]),
                            r["contact"], r["phone"], r["email"], r["address"],
                            r["tax_no"], r["payment_terms_days"], r["credit_limit"],
                            r["remark"] if "remark" in r.keys() else ""])
        elif kind == "opening_stock":
            w.writerow(["SKU", "名称", "仓库", "数量", "单位成本"])
            for r in conn.execute(
                "SELECT p.sku,p.name,w.name wh,i.qty,i.avg_cost"
                " FROM inventory i JOIN products p ON p.id=i.product_id"
                " JOIN warehouses w ON w.id=i.warehouse_id ORDER BY p.sku"):
                w.writerow([r["sku"], r["name"], r["wh"], r["qty"], r["avg_cost"]])
        else:
            raise ImportError_(f"不支持的导出类型：{kind}")
    return _write_text(path, "﻿" + buf.getvalue())
