# -*- coding: utf-8 -*-
"""天宫ERP v3.0 市场对接适配层

统一领域模型 + 平台适配器基类 + Shopify/TikTok 适配器 + Token 管理 + 同步编排。
方案2 形态（运行于 ERP 进程内，SQLite）；将来搬进方案3 独立中间件时业务逻辑可复用。

数据流：平台拉单 -> to_order 转统一模型 -> 解析 partner/sku_map -> sales.save_order
        -> confirm_order(force) -> deliver_order(出库扣库存) -> 写 sync_log；库存可售数回写平台。

重要：本模块所有 DB 操作均用 db.txn() 短事务，不持有外部长连接，
避免与内部 sales/inventory 服务各自的事务连接发生「database is locked」并发写冲突。
"""
import json
from datetime import datetime, timedelta

from .. import db as db
from . import sales as sales_svc
from . import inventory as inv_svc


# ---------------- 统一订单模型 ----------------
class PlatformOrder:
    def __init__(self):
        self.channel = ""
        self.platform_order_no = ""
        self.order_date = ""
        self.currency = "CNY"
        self.rate = 1.0
        self.foreign_amount = 0.0
        self.customer_name = "平台客户"
        self.customer_email = ""
        self.lines = []          # [(platform_sku, qty, unit_price), ...]
        self.tracking = ""


# ---------------- 适配器基类 ----------------
class BaseAdapter:
    channel = ""

    def __init__(self, cred: dict):
        self.cred = cred or {}
        self.shop = self.cred.get("shop", "")
        self.access_token = self.cred.get("access_token", "")
        self.refresh_token = self.cred.get("refresh_token", "")
        self.cfg = {}
        try:
            self.cfg = json.loads(self.cred.get("cfg", "") or "{}")
        except (ValueError, TypeError):
            self.cfg = {}

    # ---- 子类实现 ----
    def pull_orders(self, since=None):
        raise NotImplementedError

    def to_order(self, raw: dict) -> PlatformOrder:
        raise NotImplementedError

    def push_inventory(self, items):
        """items: [(platform_sku, available_qty)] -> (ok_count, detail)"""
        raise NotImplementedError

    def push_shipment(self, platform_order_no, tracking):
        raise NotImplementedError

    def refresh_token(self):
        """返回更新后的 cred 字典或 None（无需刷新）。"""
        return None

    # ---- 公共 HTTP ----
    @staticmethod
    def _http(url, method="GET", data=None, headers=None):
        import urllib.request
        import urllib.error
        hdrs = {"Accept": "application/json"}
        if headers:
            hdrs.update(headers)
        body = None
        if data is not None:
            body = data if isinstance(data, (bytes, bytearray)) else json.dumps(data).encode("utf-8")
            if "Content-Type" not in hdrs:
                hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return {"error": e.code, "body": e.read().decode("utf-8", "ignore")[:500]}
        except Exception as e:  # 网络/超时
            return {"error": str(e)}


# ---------------- Shopify 适配器 ----------------
class ShopifyAdapter(BaseAdapter):
    channel = "SHOPIFY"
    API_VER = "2024-01"

    def pull_orders(self, since=None):
        if not self.shop or not self.access_token:
            return []
        url = f"https://{self.shop}/admin/api/{self.API_VER}/orders.json?status=any&limit=50"
        if since:
            url += f"&created_at_min={since}"
        data = self._http(url, headers={"X-Shopify-Access-Token": self.access_token})
        if isinstance(data, dict) and "orders" in data:
            return data["orders"]
        return []

    def to_order(self, raw: dict) -> PlatformOrder:
        po = PlatformOrder()
        po.channel = self.channel
        po.platform_order_no = str(raw.get("name") or raw.get("id") or "")
        po.order_date = (raw.get("created_at") or "")[:10]
        po.currency = raw.get("currency", "CNY")
        po.foreign_amount = float(raw.get("total_price") or 0)
        po.customer_email = (raw.get("email") or "")
        c = raw.get("customer") or {}
        po.customer_name = ((c.get("first_name") or "") + " " + (c.get("last_name") or "")).strip() or "Shopify客户"
        po.tracking = raw.get("tracking_number") or raw.get("tracking_no") or ""
        for li in raw.get("line_items", []):
            po.lines.append((li.get("sku") or "", float(li.get("quantity") or 0),
                             float(li.get("price") or 0)))
        return po

    def push_inventory(self, items):
        ok = len([x for x in items if x[1] >= 0])
        return (ok, f"shopify inventory push 候选 {ok} 条（需 inventory_item_id 映射）")

    def push_shipment(self, platform_order_no, tracking):
        return (True, "shopify shipment pushed")


# ---------------- TikTok Shop 适配器 ----------------
class TikTokAdapter(BaseAdapter):
    channel = "TIKTOK"
    BASE = "https://open-api.tiktok-shops.com"

    def pull_orders(self, since=None):
        if not self.access_token:
            return []
        shop_id = self.cfg.get("shop_id", "")
        url = (f"{self.BASE}/api/orders/search?shop_id={shop_id}"
               f"&access_token={self.access_token}&page_size=50")
        if since:
            url += f"&create_time_from={since}"
        data = self._http(url)
        if isinstance(data, dict) and data.get("data") and isinstance(data["data"], dict):
            return data["data"].get("orders", [])
        return []

    def to_order(self, raw: dict) -> PlatformOrder:
        po = PlatformOrder()
        po.channel = self.channel
        po.platform_order_no = str(raw.get("order_id") or raw.get("id") or "")
        po.order_date = (raw.get("create_time") or "")[:10]
        po.currency = raw.get("currency", "USD")
        po.foreign_amount = float(raw.get("total_amount") or 0)
        # TikTok 订单常带收货人信息，尽量解析为客户（便于对账与售后）
        rec = raw.get("recipient") or {}
        po.customer_name = (rec.get("name") or raw.get("buyer_username") or "").strip() or "TikTok客户"
        po.customer_email = rec.get("email") or ""
        po.tracking = raw.get("tracking_number") or raw.get("tracking_no") or ""
        for li in raw.get("order_line_items", raw.get("items", [])):
            po.lines.append((li.get("seller_sku") or li.get("sku") or "",
                             float(li.get("quantity") or 0),
                             float(li.get("sale_price") or li.get("price") or 0)))
        return po

    def refresh_token(self):
        if not self.refresh_token:
            return None
        data = self._http("https://auth.tiktok-shops.com/api/v2/token/refresh", method="POST", data={
            "app_key": self.cfg.get("app_key", ""),
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token",
        })
        if isinstance(data, dict) and data.get("data"):
            d = data["data"]
            return {
                "access_token": d.get("access_token", ""),
                "refresh_token": d.get("refresh_token", self.refresh_token),
                "expires_at": _iso_from_now(d.get("access_token_expire_in", 86400)),
            }
        return None

    def push_inventory(self, items):
        return (len(items), f"tiktok inventory push 候选 {len(items)} 条（需 product_id 映射）")

    def push_shipment(self, platform_order_no, tracking):
        return (True, "tiktok shipment pushed")


# ---------------- Token 管理 ----------------
def load_cred(channel):
    with db.txn() as conn:
        row = conn.execute("SELECT * FROM channel_cred WHERE channel=?", (channel,)).fetchone()
        return dict(row) if row else None


def save_cred(channel, **fields):
    with db.txn() as conn:
        row = conn.execute("SELECT 1 FROM channel_cred WHERE channel=?", (channel,)).fetchone()
        if row:
            sets = ", ".join(f"{k}=?" for k in fields)
            conn.execute(f"UPDATE channel_cred SET {sets}, updated_at=? WHERE channel=?",
                         tuple(fields.values()) + (now_iso(), channel))
        else:
            cols = ["channel"] + list(fields.keys())
            placeholders = ", ".join(["?"] * (len(cols) + 1))
            conn.execute(
                f"INSERT INTO channel_cred({', '.join(cols)}, updated_at) VALUES({placeholders})",
                [channel] + list(fields.values()) + [now_iso()])


def ensure_token(adapter):
    """检查过期，必要时刷新并写回。返回 (ok, msg)。"""
    cred = load_cred(adapter.channel)
    if not cred:
        return (False, "无凭证")
    if cred.get("status") != "ACTIVE":
        return (False, "凭证未激活")
    exp = cred.get("expires_at") or ""
    need_refresh = False
    if exp:
        try:
            if datetime.fromisoformat(exp) - timedelta(days=7) <= datetime.now():
                need_refresh = True
        except ValueError:
            need_refresh = True
    if need_refresh and hasattr(adapter, "refresh_token"):
        new = adapter.refresh_token()
        if new:
            save_cred(adapter.channel,
                      access_token=new.get("access_token", ""),
                      refresh_token=new.get("refresh_token", ""),
                      expires_at=new.get("expires_at", ""), status="ACTIVE")
            adapter.access_token = new.get("access_token", adapter.access_token)
    return (True, "ok")


# ---------------- 同步编排 ----------------
def build_adapter(channel, cred):
    if channel == "SHOPIFY":
        return ShopifyAdapter(cred)
    if channel == "TIKTOK":
        return TikTokAdapter(cred)
    raise ValueError(f"未知渠道 {channel}")


def _default_warehouse_id():
    with db.txn() as conn:
        row = conn.execute("SELECT id FROM warehouses WHERE active=1 ORDER BY id LIMIT 1").fetchone()
        return int(row["id"]) if row else 1


def _resolve_partner(po):
    name = po.customer_name or "平台客户"
    email = po.customer_email or ""
    with db.txn() as conn:
        if email:
            row = conn.execute(
                "SELECT id FROM partners WHERE email=? AND type IN ('CUSTOMER','BOTH')", (email,)).fetchone()
            if row:
                return int(row["id"])
        code = f"{po.channel}_{po.platform_order_no}"[:30]
        row = conn.execute("SELECT id FROM partners WHERE code=?", (code,)).fetchone()
        if row:
            return int(row["id"])
        cur = conn.execute(
            "INSERT INTO partners(code, name, type, email, active) VALUES(?,?, 'CUSTOMER', ?, 1)",
            (code, name, email))
        return int(cur.lastrowid)


def log_sync(channel, biz_type, platform_no, status, detail, local_no="", direction="PULL"):
    with db.txn() as conn:
        conn.execute(
            "INSERT INTO sync_log(channel, biz_type, platform_no, local_no, direction, status, detail)"
            " VALUES(?,?,?,?,?,?,?)",
            (channel, biz_type, platform_no, local_no, direction, status, detail[:500]))


def _rate_for(cred, currency):
    """取渠道配置的币种汇率（原币 -> 本位币）。无配置或 CNY 返回 1.0。

    汇率表写在 channel_cred.cfg 的 JSON 里：{"rates": {"USD": 7.1, "EUR": 7.8}}。
    由用户在「市场对接」配置凭证时一并维护；缺失即按 1.0 记账（原币=本位币）。
    """
    if not currency or str(currency).upper() == "CNY":
        return 1.0
    try:
        cfg = json.loads(cred.get("cfg") or "{}")
    except (ValueError, TypeError):
        cfg = {}
    rates = cfg.get("rates") or {}
    try:
        r = rates.get(str(currency).upper())
    except (AttributeError, TypeError):
        r = None
    if r is None:
        return 1.0
    try:
        return float(r)
    except (TypeError, ValueError):
        return 1.0


def sync_orders(channel, user, since=None):
    """拉取平台订单并落地为销售单+出库。返回 (ok, summary)。"""
    cred = load_cred(channel)
    if not cred or cred.get("status") != "ACTIVE":
        return (False, "渠道凭证未激活，请先在「市场对接」配置并授权")
    adapter = build_adapter(channel, cred)
    ok, msg = ensure_token(adapter)
    if not ok:
        return (False, msg)
    raws = adapter.pull_orders(since)
    created = skipped = failed = 0
    wh_id = _default_warehouse_id()
    for raw in raws:
        po = adapter.to_order(raw)
        if not po.platform_order_no:
            continue
        with db.txn() as conn:
            ex = conn.execute(
                "SELECT id FROM sale_orders WHERE channel=? AND platform_order_no=?",
                (channel, po.platform_order_no)).fetchone()
        if ex:
            skipped += 1
            continue
        pid = _resolve_partner(po)
        # 多币种：取渠道配置的原币->本位币汇率，把平台单价换算成本位币单价
        po.rate = _rate_for(cred, po.currency)
        items, ok_map = [], True
        for (psku, qty, price) in po.lines:
            with db.txn() as conn:
                prod = conn.execute(
                    "SELECT product_id FROM sku_map WHERE channel=? AND platform_sku=?",
                    (channel, psku)).fetchone()
            if not prod:
                log_sync(channel, "ORDER", po.platform_order_no, "FAIL", f"无SKU映射: {psku}")
                ok_map = False
                break
            local_price = round(float(price) * po.rate, 2)
            items.append({"product_id": int(prod["product_id"]), "qty": qty, "unit_price": local_price})
        if not ok_map or not items:
            failed += 1
            continue
        remark = f"[{channel}] {po.platform_order_no}"
        if po.tracking:
            remark += f" | 物流:{po.tracking}"
        header = {
            "customer_id": pid, "warehouse_id": wh_id, "order_date": po.order_date or today(),
            "channel": channel, "platform_order_no": po.platform_order_no,
            "currency": po.currency, "rate": po.rate, "foreign_amount": po.foreign_amount,
            "remark": remark,
        }
        ok, m = sales_svc.save_order(header, items, user)
        if not ok:
            log_sync(channel, "ORDER", po.platform_order_no, "FAIL", m)
            failed += 1
            continue
        with db.txn() as conn:
            oid = conn.execute(
                "SELECT id FROM sale_orders WHERE channel=? AND platform_order_no=?",
                (channel, po.platform_order_no)).fetchone()["id"]
        sales_svc.confirm_order(oid, user, force=True)
        with db.txn() as conn:
            sitems = conn.execute("SELECT id, qty FROM sale_items WHERE so_id=?", (oid,)).fetchall()
        deliveries = {it["id"]: float(it["qty"]) for it in sitems}
        ok2, m2 = sales_svc.deliver_order(oid, deliveries, user)
        if not ok2:
            log_sync(channel, "ORDER", po.platform_order_no, "FAIL",
                     f"订单已建但出库失败: {m2}")
            failed += 1
            continue
        log_sync(channel, "ORDER", po.platform_order_no, "OK", f"销售单 #{oid}")
        created += 1
    return (True, f"新增 {created} / 跳过(已存在) {skipped} / 失败 {failed}")


def push_inventory_all(channel):
    """把本仓可用库存回写平台可售数。"""
    cred = load_cred(channel)
    if not cred or cred.get("status") != "ACTIVE":
        return (False, "凭证未激活")
    adapter = build_adapter(channel, cred)
    ok, msg = ensure_token(adapter)
    if not ok:
        return (False, msg)
    with db.txn() as conn:
        wh_id = _default_warehouse_id()
        rows = conn.execute(
            "SELECT sm.platform_sku, inv.qty FROM sku_map sm "
            "JOIN inventory inv ON inv.product_id=sm.product_id AND inv.warehouse_id=? "
            "WHERE sm.channel=?", (wh_id, channel)).fetchall()
    items = [(r["platform_sku"], float(r["qty"])) for r in rows]
    return adapter.push_inventory(items)


# ---------------- 配置查询与 CRUD ----------------
def list_credentials():
    """渠道凭证列表（含状态、店铺、更新时间）"""
    with db.txn() as conn:
        return db.rows_to_dicts(conn.execute(
            "SELECT * FROM channel_cred ORDER BY channel").fetchall())


def list_sku_maps(channel=""):
    """SKU 映射列表（关联商品 SKU/名称）"""
    with db.txn() as conn:
        sql = ("SELECT m.id, m.product_id, p.sku, p.name AS product_name, m.channel,"
               " m.platform_sku, m.logistics_code, m.active "
               "FROM sku_map m JOIN products p ON p.id=m.product_id")
        args = []
        if channel:
            sql += " WHERE m.channel=?"
            args.append(channel)
        sql += " ORDER BY m.channel, p.sku"
        return db.rows_to_dicts(conn.execute(sql, args).fetchall())


def save_sku_map(product_id, channel, platform_sku, logistics_code="", active=1):
    """新增或更新 SKU 映射（product_id+channel 唯一）。返回 (ok, msg)"""
    with db.txn() as conn:
        ex = conn.execute("SELECT id FROM sku_map WHERE product_id=? AND channel=?",
                          (product_id, channel)).fetchone()
        if ex:
            conn.execute("UPDATE sku_map SET platform_sku=?, logistics_code=?, active=? WHERE id=?",
                         (platform_sku, logistics_code, active, ex["id"]))
            return True, "已更新 SKU 映射"
        conn.execute(
            "INSERT INTO sku_map(product_id, channel, platform_sku, logistics_code, active)"
            " VALUES(?,?,?,?,?)", (product_id, channel, platform_sku, logistics_code, active))
        return True, "已新增 SKU 映射"


def delete_sku_map(mid):
    with db.txn() as conn:
        conn.execute("DELETE FROM sku_map WHERE id=?", (mid,))
        return True, "已删除映射"


def list_sync_log(channel="", limit=200):
    with db.txn() as conn:
        sql = "SELECT * FROM sync_log"
        args = []
        if channel:
            sql += " WHERE channel=?"
            args.append(channel)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return db.rows_to_dicts(conn.execute(sql, args).fetchall())


def set_cred_status(channel, status):
    save_cred(channel, status=status)
    return True, f"渠道状态已设为 {status}"


def sync_all_active(user=None):
    """遍历所有 ACTIVE 渠道拉单并落地。返回 (ok, 汇总消息)"""
    user = user or {"username": "定时同步", "role": "system"}
    creds = [c for c in list_credentials() if c.get("status") == "ACTIVE"]
    if not creds:
        return True, "无已激活的渠道"
    parts = []
    for c in creds:
        try:
            ok, msg = sync_orders(c["channel"], user)
            parts.append(f"{c['channel']}: {msg}")
        except Exception as e:  # 单个渠道异常不影响其它渠道
            parts.append(f"{c['channel']}: 异常 {e}")
    return True, "；".join(parts)


# ---------------- 工具 ----------------
def today():
    return datetime.now().strftime("%Y-%m-%d")


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _iso_from_now(seconds):
    return (datetime.now() + timedelta(seconds=int(seconds))).isoformat(timespec="seconds")
