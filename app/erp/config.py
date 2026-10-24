# -*- coding: utf-8 -*-
"""天宫ERP系统 —— 配置模块

所有全局参数、路径、单据状态枚举、角色权限集中在此处定义。
"""
from pathlib import Path
import os
import json

APP_NAME = "天宫ERP系统"
APP_VERSION = "1.0.0"
COMPANY_NAME = "天宫科技"

# ---------------------------------------------------------------- 路径配置
import sys

# 源码运行：项目根目录 = erp/ 包的上一层
# 打包成 exe（PyInstaller/Nuitka 的 frozen 模式）：根目录 = exe 所在目录，
# 使用 sys.argv[0] 取到原始 exe 路径，避免 onefile 模式指向临时解压目录，
# 保证 data/ 落在 exe 旁边而不是临时目录，数据才能真正持久化
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.argv[0]).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("ERP_DATA_DIR", BASE_DIR / "data"))
DB_PATH = Path(os.environ.get("ERP_DB", DATA_DIR / "erp.db"))
EXPORT_DIR = DATA_DIR / "exports"
LOG_DIR = DATA_DIR / "logs"

for _d in (DATA_DIR, EXPORT_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- 分页 / 格式
PAGE_SIZE = 20
CURRENCY = "¥"
DATE_FMT = "%Y-%m-%d"

# 内置管理员账号（首次启动自动创建）
DEFAULT_ADMIN = ("admin", "admin123")

# ---------------------------------------------------------------- 单据状态
PO_STATUS = {
    "DRAFT": "草稿",
    "CONFIRMED": "已确认",
    "PARTIAL": "部分入库",
    "COMPLETED": "已完成",
    "CANCELLED": "已取消",
}
SO_STATUS = {
    "DRAFT": "草稿",
    "CONFIRMED": "已确认",
    "PARTIAL": "部分出库",
    "COMPLETED": "已完成",
    "CANCELLED": "已取消",
}
# 退货单状态（v2.2.0）：销售退货=客户把货退回公司；采购退货=把货退给供应商
RT_STATUS = {
    "DRAFT": "草稿",
    "CONFIRMED": "已确认",
    "DONE": "已执行",
    "CANCELLED": "已取消",
}
RT_TYPE = {
    "SALE_RETURN": "销售退货",
    "PURCHASE_RETURN": "采购退货",
}
PARTNER_TYPE = {
    "CUSTOMER": "客户",
    "SUPPLIER": "供应商",
    "BOTH": "客户/供应商",
}
MOVE_TYPE = {"IN": "入库", "OUT": "出库", "ADJ": "调整"}
PAY_DIRECTION = {"RECEIVE": "收款", "PAY": "付款"}
PAY_METHOD = ["银行转账", "现金", "承兑汇票", "支付宝", "微信", "其他"]
ABC_CLASS = {"A": "A类(重点)", "B": "B类(一般)", "C": "C类(次要)"}

# ---------------------------------------------------------------- 指标参数
# 这些是系统计算 KPI 时使用的默认经营参数，可在「系统设置」中调整
DEFAULT_PARAMS = {
    "order_cost": 200.0,          # 每次订货成本 S（用于 EOQ）
    "holding_rate": 0.25,         # 年持有成本率（占单价比例，用于 EOQ）
    "days_in_year": 365,          # 年化天数
    "demand_window_days": 90,     # 日均需求统计窗口
    "turnover_window_days": 90,   # 库存周转率统计窗口
    "dull_days": 120,             # 呆滞库存判定天数（无出库）
    "service_level_z": 1.65,      # 服务水平 Z 值（95%），用于安全库存
    "gross_margin_target": 0.25,  # 目标毛利率
    "stockout_risk_level": 1.0,   # 缺货风险线：可用量 <= 安全库存 视为风险
}

# ---------------------------------------------------------------- 角色权限
# 模块键 -> 权限：r 只读，w 读写
MODULES = [
    ("dashboard", "经营看板"),
    ("products", "商品资料"),
    ("partners", "往来单位"),
    ("inventory", "库存管理"),
    ("warehouse", "仓库可视化"),
    ("purchase", "采购管理"),
    ("sales", "销售管理"),
    ("returns", "退货管理"),
    ("marketplace", "市场对接"),
    ("finance", "收付款"),
    ("reports", "报表分析"),
    ("users", "用户权限"),
    ("logs", "操作日志"),
]

ROLE_PERMS = {
    "admin": {m: "w" for m, _ in MODULES},
    "manager": {
        "dashboard": "w", "products": "w", "partners": "w", "inventory": "w",
        "warehouse": "w", "purchase": "w", "sales": "w", "finance": "w", "reports": "w",
        "returns": "w", "marketplace": "w",
        "users": "r", "logs": "r",
    },
    "sales": {
        "dashboard": "r", "products": "r", "partners": "w", "inventory": "r",
        "warehouse": "r", "sales": "w", "returns": "w", "marketplace": "w", "finance": "r", "reports": "r",
    },
    "purchase": {
        "dashboard": "r", "products": "r", "partners": "w", "inventory": "r",
        "warehouse": "r", "purchase": "w", "returns": "w", "marketplace": "w", "reports": "r",
    },
    "warehouse": {
        "dashboard": "r", "products": "r", "inventory": "w",
        "warehouse": "w", "purchase": "r", "sales": "r", "returns": "w", "marketplace": "w", "reports": "r",
    },
    "finance": {
        "dashboard": "r", "partners": "r", "inventory": "r",
        "warehouse": "r", "purchase": "r", "sales": "r", "returns": "w", "marketplace": "w", "finance": "w", "reports": "w",
    },
    "viewer": {m: "r" for m, _ in MODULES},
}

ROLE_NAMES = {
    "admin": "系统管理员",
    "manager": "总经理",
    "sales": "销售",
    "purchase": "采购",
    "warehouse": "仓库",
    "finance": "财务",
    "viewer": "只读访客",
}

# 分区（模块）权限级别：none=不开放，r=只读，w=可编辑
PERM_NONE, PERM_READ, PERM_WRITE = "none", "r", "w"
PERM_LEVELS = [
    (PERM_NONE, "不开放"),
    (PERM_READ, "只读"),
    (PERM_WRITE, "可编辑"),
]
PERM_TEXT = dict(PERM_LEVELS)

# 角色说明（新建账号时作为权限模板的提示文案）
ROLE_DESC = {
    "admin": "系统全部功能，且不可被限制",
    "manager": "全部业务模块可编辑，用户与日志只读",
    "sales": "销售订单、客户可编辑，其余只读",
    "purchase": "采购订单、供应商可编辑，其余只读",
    "warehouse": "库存、商品可编辑，单据只读",
    "finance": "收付款、报表可编辑，单据只读",
    "viewer": "全部模块只读",
}

# 市场对接（v3.0）：方案2 起步配置。将来搬方案3（独立同步中间件 + PostgreSQL 中心库）时，
# 本段配置原样迁移到中间件侧即可，业务字段含义不变。
MARKETPLACE = {
    # 已支持的对接渠道（新增渠道只需在 services/marketplace.py 加对应 Adapter 并登记此处）
    "channels": ["SHOPIFY", "TIKTOK"],
    # Web 后台定时同步间隔（分钟）。<=0 表示不启用后台定时同步，仅可手动触发。
    "sync_interval_min": 30,
    # 是否在每次成功拉单后，把本仓可用库存回写平台可售数
    "auto_push_inventory": True,
    # 单页同步日志保留条数（仅影响展示，不清理库）
    "sync_log_limit": 200,
}


def parse_perms(raw):
    """把数据库里的 perms 字符串解析成 {模块: r/w} 字典。

    返回 None 表示「未自定义」，此时沿用该角色在 ROLE_PERMS 中的默认权限。
    """
    if not raw:
        return None
    if isinstance(raw, dict):
        raw = json.dumps(raw)
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not data:
        return None
    return {k: v for k, v in data.items() if v in (PERM_READ, PERM_WRITE)} or None


def dump_perms(perms) -> str:
    """把权限字典序列化为存入数据库的字符串（无自定义时存空串）"""
    if not perms:
        return ""
    if isinstance(perms, str):
        return perms
    clean = {k: v for k, v in perms.items() if v in (PERM_READ, PERM_WRITE)}
    return json.dumps(clean, ensure_ascii=False) if clean else ""


def effective_perms(user: dict) -> dict:
    """用户最终生效的分区权限（自定义优先，否则用角色默认）"""
    if not user:
        return {}
    if user.get("role") == "admin":
        return {m: PERM_WRITE for m, _ in MODULES}
    return parse_perms(user.get("perms")) or dict(ROLE_PERMS.get(user.get("role", "viewer"), {}))


def can(user: dict, module: str, write: bool = False) -> bool:
    """判断用户是否具备某分区（模块）权限。user 为会话字典。"""
    if not user:
        return False
    if user.get("role") == "admin":          # 内置管理员恒为全权限，避免把自己锁死
        return True
    perms = parse_perms(user.get("perms"))
    if perms is None:                        # 未自定义 → 沿用角色默认矩阵
        perms = ROLE_PERMS.get(user.get("role", "viewer"), {})
    level = perms.get(module)
    if not level:
        return False
    if write:
        return level == PERM_WRITE
    return level in (PERM_READ, PERM_WRITE)
