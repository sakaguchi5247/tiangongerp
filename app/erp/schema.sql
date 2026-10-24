-- ============================================================
-- 简易 ERP 数据库结构（SQLite）
-- 覆盖：组织权限 / 主数据 / 库存 / 采购 / 销售 / 资金 / 日志
-- ============================================================

-- ---------------- 组织与权限 ----------------
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    full_name     TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    salt          TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'viewer',
    perms         TEXT NOT NULL DEFAULT '',   -- 自定义分区权限 JSON，如 {"inventory":"w","purchase":"r"}；空=沿用角色默认
    must_change   INTEGER NOT NULL DEFAULT 0, -- 1=下次登录必须修改口令（出厂默认口令用）
    active        INTEGER NOT NULL DEFAULT 1,
    last_login    TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS sys_params (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    note  TEXT DEFAULT ''
);

-- ---------------- 主数据 ----------------
CREATE TABLE IF NOT EXISTS categories (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS warehouses (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    code    TEXT NOT NULL UNIQUE,
    name    TEXT NOT NULL,
    address TEXT DEFAULT '',
    active  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS products (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    sku            TEXT NOT NULL UNIQUE,
    name           TEXT NOT NULL,
    category_id    INTEGER REFERENCES categories(id),
    spec           TEXT DEFAULT '',
    unit           TEXT NOT NULL DEFAULT '件',
    barcode        TEXT DEFAULT '',
    cost_price     REAL NOT NULL DEFAULT 0,     -- 参考成本价（最新采购价）
    sale_price     REAL NOT NULL DEFAULT 0,     -- 标准销售价
    safety_stock   REAL NOT NULL DEFAULT 0,     -- 安全库存
    lead_time_days INTEGER NOT NULL DEFAULT 7,  -- 采购提前期(天)
    reorder_point  REAL NOT NULL DEFAULT 0,     -- 再订货点 ROP
    abc_class      TEXT NOT NULL DEFAULT 'C',   -- ABC 分类
    active         INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS partners (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    code              TEXT NOT NULL UNIQUE,
    name              TEXT NOT NULL,
    type              TEXT NOT NULL DEFAULT 'CUSTOMER',   -- CUSTOMER/SUPPLIER/BOTH
    contact           TEXT DEFAULT '',
    phone             TEXT DEFAULT '',
    email             TEXT DEFAULT '',
    address           TEXT DEFAULT '',
    tax_no            TEXT DEFAULT '',
    payment_terms_days INTEGER NOT NULL DEFAULT 30,       -- 账期(天)
    credit_limit      REAL NOT NULL DEFAULT 0,            -- 信用额度
    remark        TEXT DEFAULT '',            -- 备注（v1.5.1 新增：导入模板含该列，过去会被静默丢弃）
    active            INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- ---------------- 库存 ----------------
CREATE TABLE IF NOT EXISTS inventory (
    product_id   INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    warehouse_id INTEGER NOT NULL REFERENCES warehouses(id) ON DELETE CASCADE,
    qty          REAL NOT NULL DEFAULT 0,
    avg_cost     REAL NOT NULL DEFAULT 0,            -- 移动加权平均成本
    updated_at   TEXT,
    PRIMARY KEY (product_id, warehouse_id)
);

CREATE TABLE IF NOT EXISTS stock_moves (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    move_no       TEXT NOT NULL,
    move_date     TEXT NOT NULL,
    move_type     TEXT NOT NULL,                     -- IN / OUT / ADJ
    direction     INTEGER NOT NULL,                  -- +1 入库 / -1 出库
    product_id    INTEGER NOT NULL REFERENCES products(id),
    warehouse_id  INTEGER NOT NULL REFERENCES warehouses(id),
    qty           REAL NOT NULL,                     -- 正数
    unit_cost     REAL NOT NULL DEFAULT 0,
    amount        REAL NOT NULL DEFAULT 0,
    balance_after REAL NOT NULL DEFAULT 0,
    zone_id       INTEGER REFERENCES zones(id),       -- 库区（v1.6.0 仓库分区可视化）
    ref_type      TEXT DEFAULT '',                   -- PURCHASE / SALE / ADJUST / TRANSFER
    ref_no        TEXT DEFAULT '',
    ref_id        INTEGER,
    remark        TEXT DEFAULT '',
    created_by    TEXT DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_moves_product ON stock_moves(product_id, move_date);
CREATE INDEX IF NOT EXISTS idx_moves_ref ON stock_moves(ref_type, ref_id);

-- ---------------- 采购 ----------------
CREATE TABLE IF NOT EXISTS purchase_orders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    po_no         TEXT NOT NULL UNIQUE,
    supplier_id   INTEGER NOT NULL REFERENCES partners(id),
    warehouse_id  INTEGER NOT NULL REFERENCES warehouses(id),
    order_date    TEXT NOT NULL,
    expected_date TEXT,
    status        TEXT NOT NULL DEFAULT 'DRAFT',
    total_amount  REAL NOT NULL DEFAULT 0,
    paid_amount   REAL NOT NULL DEFAULT 0,
    currency      TEXT NOT NULL DEFAULT 'CNY',
    rate          REAL NOT NULL DEFAULT 1,
    foreign_amount REAL NOT NULL DEFAULT 0,
    remark        TEXT DEFAULT '',
    created_by    TEXT DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS purchase_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    po_id        INTEGER NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
    product_id   INTEGER NOT NULL REFERENCES products(id),
    qty          REAL NOT NULL DEFAULT 0,
    unit_price   REAL NOT NULL DEFAULT 0,
    amount       REAL NOT NULL DEFAULT 0,
    received_qty REAL NOT NULL DEFAULT 0,
    received_at  TEXT DEFAULT '',            -- 最后一次收货日期（v1.5.1：报表按实际入库日归月）
    sort_no      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_po_items ON purchase_items(po_id);

-- ---------------- 销售 ----------------
CREATE TABLE IF NOT EXISTS sale_orders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    so_no         TEXT NOT NULL UNIQUE,
    customer_id   INTEGER NOT NULL REFERENCES partners(id),
    warehouse_id  INTEGER NOT NULL REFERENCES warehouses(id),
    order_date    TEXT NOT NULL,
    deliver_date  TEXT,
    status        TEXT NOT NULL DEFAULT 'DRAFT',
    total_amount  REAL NOT NULL DEFAULT 0,
    paid_amount   REAL NOT NULL DEFAULT 0,
    channel       TEXT NOT NULL DEFAULT '',        -- 来源平台 SHOPIFY/TIKTOK/AMAZON/空=手工单
    platform_order_no TEXT NOT NULL DEFAULT '',    -- 平台原始单号（幂等去重用）
    currency      TEXT NOT NULL DEFAULT 'CNY',     -- 币种（v3.0 多币种）
    rate          REAL NOT NULL DEFAULT 1,         -- 汇率（原币->本位币）
    foreign_amount REAL NOT NULL DEFAULT 0,        -- 原币金额
    remark        TEXT DEFAULT '',
    created_by    TEXT DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS sale_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    so_id         INTEGER NOT NULL REFERENCES sale_orders(id) ON DELETE CASCADE,
    product_id    INTEGER NOT NULL REFERENCES products(id),
    qty           REAL NOT NULL DEFAULT 0,
    unit_price    REAL NOT NULL DEFAULT 0,
    unit_cost     REAL NOT NULL DEFAULT 0,     -- 出库时记录的加权成本
    amount        REAL NOT NULL DEFAULT 0,
    delivered_qty REAL NOT NULL DEFAULT 0,
    delivered_at  TEXT DEFAULT '',      -- 最后一次出库日期（v1.5.1：收入改按出库日确认）
    sort_no       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_so_items ON sale_items(so_id);

-- ---------------- 退货（v2.2.0） ----------------
-- 销售退货：客户把货退回公司（入库退货区，红冲应收）
-- 采购退货：把货退给供应商（出库，红冲应付）
CREATE TABLE IF NOT EXISTS return_orders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    rt_no         TEXT NOT NULL UNIQUE,
    rt_type       TEXT NOT NULL DEFAULT 'SALE_RETURN',   -- SALE_RETURN 销售退货 / PURCHASE_RETURN 采购退货
    ref_type      TEXT NOT NULL DEFAULT '',              -- 关联原单类型 SALE / PURCHASE
    ref_id        INTEGER,                               -- 关联原单 id（sale_orders / purchase_orders）
    partner_id    INTEGER NOT NULL REFERENCES partners(id),
    warehouse_id  INTEGER NOT NULL REFERENCES warehouses(id),
    order_date    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'DRAFT',
    total_amount  REAL NOT NULL DEFAULT 0,
    remark        TEXT DEFAULT '',
    created_by    TEXT DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS return_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    rto_id       INTEGER NOT NULL REFERENCES return_orders(id) ON DELETE CASCADE,
    product_id   INTEGER NOT NULL REFERENCES products(id),
    ref_line_id  INTEGER,                               -- 关联原单明细行 id（sale_items / purchase_items）
    qty          REAL NOT NULL DEFAULT 0,
    unit_price   REAL NOT NULL DEFAULT 0,              -- 退货单价（默认取原单单价，可改，用于红冲金额）
    amount       REAL NOT NULL DEFAULT 0,
    sort_no      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_rto_items ON return_items(rto_id);
CREATE INDEX IF NOT EXISTS idx_rto_ref ON return_orders(ref_type, ref_id);

-- ---------------- 市场对接（v3.0 海外电商平台 API） ----------------
-- SKU 三级映射：内部商品 <-> 平台 SKU <-> 物流编码
CREATE TABLE IF NOT EXISTS sku_map (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id    INTEGER NOT NULL REFERENCES products(id),
    channel       TEXT NOT NULL,                 -- SHOPIFY / TIKTOK / AMAZON
    platform_sku  TEXT NOT NULL DEFAULT '',
    logistics_code TEXT DEFAULT '',
    extra         TEXT DEFAULT '',
    active        INTEGER NOT NULL DEFAULT 1,
    UNIQUE(product_id, channel)
);
CREATE INDEX IF NOT EXISTS idx_sku_map_channel ON sku_map(channel, platform_sku);

-- 平台凭证（OAuth Token 加密存储，不落明文）
CREATE TABLE IF NOT EXISTS channel_cred (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    channel       TEXT NOT NULL UNIQUE,          -- SHOPIFY / TIKTOK
    shop          TEXT DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'INACTIVE',  -- ACTIVE / INACTIVE / EXPIRED
    access_token  TEXT DEFAULT '',
    refresh_token TEXT DEFAULT '',
    token_secret  TEXT DEFAULT '',
    expires_at    TEXT DEFAULT '',               -- ISO 时间
    cfg           TEXT DEFAULT '',              -- JSON: shop/API 配置
    note          TEXT DEFAULT '',
    updated_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 同步日志（幂等 + 对账）
CREATE TABLE IF NOT EXISTS sync_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel     TEXT NOT NULL,
    biz_type    TEXT NOT NULL,                   -- ORDER / INVENTORY / SHIPMENT / REFUND
    platform_no TEXT DEFAULT '',
    local_no    TEXT DEFAULT '',
    direction   TEXT NOT NULL DEFAULT 'PULL',    -- PULL / PUSH
    status      TEXT NOT NULL DEFAULT 'OK',      -- OK / FAIL / SKIP
    detail      TEXT DEFAULT '',
    synced_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_sync_channel ON sync_log(channel, biz_type, platform_no);

-- 销售单(平台, 平台单号)幂等索引在 db._ensure_marketplace_tables 中创建：
-- 因为老库(v2.2.0)升级时建表语句被跳过，必须等 _migrate 补完 channel 列后才能建该索引，
-- 故不写进 schema.sql，避免「no such column: channel」升级失败。

-- ---------------- 资金 ----------------
CREATE TABLE IF NOT EXISTS payments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    pay_no     TEXT NOT NULL UNIQUE,
    direction  TEXT NOT NULL,                  -- RECEIVE 收款 / PAY 付款
    partner_id INTEGER NOT NULL REFERENCES partners(id),
    amount     REAL NOT NULL DEFAULT 0,
    currency   TEXT NOT NULL DEFAULT 'CNY',
    rate       REAL NOT NULL DEFAULT 1,
    foreign_amount REAL NOT NULL DEFAULT 0,
    method     TEXT DEFAULT '银行转账',
    pay_date   TEXT NOT NULL,
    ref_type   TEXT DEFAULT '',                -- SALE / PURCHASE / OTHER
    ref_id     INTEGER,
    remark     TEXT DEFAULT '',
    created_by TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_pay_partner ON payments(partner_id, direction);

-- ---------------- 日志 ----------------
CREATE TABLE IF NOT EXISTS audit_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    username   TEXT DEFAULT '',
    action     TEXT NOT NULL,
    module     TEXT NOT NULL,
    ref_no     TEXT DEFAULT '',
    detail     TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_logs_time ON audit_logs(created_at);

-- ---------------- 单号序列 ----------------
CREATE TABLE IF NOT EXISTS sequences (
    name TEXT PRIMARY KEY,
    val  INTEGER NOT NULL DEFAULT 0,
    prefix TEXT NOT NULL DEFAULT '',
    updated_on TEXT DEFAULT ''
);

-- ---------------- 仓库分区（v1.6.0 仓库可视化） ----------------
CREATE TABLE IF NOT EXISTS zones (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    warehouse_id INTEGER NOT NULL REFERENCES warehouses(id) ON DELETE CASCADE,
    code        TEXT NOT NULL,
    name        TEXT NOT NULL,
    zone_type   TEXT NOT NULL DEFAULT 'STORAGE',  -- STORAGE 存货区 / RETURN 退货区 / INSPECT 待检区 / DEFECT 残次区 / STAGING 暂存区
    capacity    REAL NOT NULL DEFAULT 0,           -- 容量（件）；0=不限制
    color       TEXT DEFAULT '',                  -- 主题色（空=按类型取默认）
    sort_order  INTEGER NOT NULL DEFAULT 0,
    active      INTEGER NOT NULL DEFAULT 1,
    UNIQUE(warehouse_id, code)
);

CREATE TABLE IF NOT EXISTS zone_stock (
    product_id   INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    warehouse_id INTEGER NOT NULL REFERENCES warehouses(id) ON DELETE CASCADE,
    zone_id      INTEGER NOT NULL REFERENCES zones(id) ON DELETE CASCADE,
    qty          REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (product_id, warehouse_id, zone_id)
);
CREATE INDEX IF NOT EXISTS idx_zone_stock_zone ON zone_stock(zone_id);

