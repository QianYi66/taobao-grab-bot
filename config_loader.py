"""配置加载器 —— 自动填充默认值，保证下游代码安全。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import yaml


def load_config(path: str = "config.yaml") -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        ex = Path("config.example.yaml")
        hint = f"请先复制: cp {ex} {p}" if ex.exists() else ""
        raise FileNotFoundError(f"配置文件不存在: {p}  {hint}")

    with p.open("r", encoding="utf-8") as f:
        cfg: Dict[str, Any] = yaml.safe_load(f) or {}

    # --- top-level ---
    cfg.setdefault("headless", False)
    cfg.setdefault("max_retries", 8)
    cfg.setdefault("retry_wait_seconds", 0.3)

    # --- browser ---
    cfg.setdefault("browser", {})
    cfg["browser"].setdefault("type", "auto")
    cfg["browser"].setdefault("user_data_dir", "")

    # --- product ---
    cfg.setdefault("product", {})
    prod = cfg["product"]
    prod.setdefault("url", "")
    prod.setdefault("sku_keywords", [])
    prod.setdefault("start_time", "")

    # --- cart ---
    cfg.setdefault("cart", {})
    cfg["cart"].setdefault("auto_checkout", True)

    # --- login ---
    cfg.setdefault("login", {})
    cfg["login"].setdefault("login_url", "https://www.taobao.com")
    cfg["login"].setdefault("cookie_path", "state/cookies.json")

    # --- keywords ---
    cfg.setdefault("keywords", {})
    kw = cfg["keywords"]
    kw.setdefault("buy_now", ["立即购买", "马上抢", "立刻购买", "立即下单", "确认订单"])
    kw.setdefault("confirm", ["提交订单", "同意协议并付款", "确认", "下一步"])

    # --- selector ---
    cfg.setdefault("selector", {})
    cfg["selector"].setdefault("buy_button_selectors", [
        "a.J_LinkBuy", "button.J_LinkBuy", "#J_LinkBuy",
        "a[href*='buy']", "button[class*='buy']",
    ])

    return cfg
