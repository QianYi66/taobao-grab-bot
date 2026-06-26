"""
淘宝/天猫抢购机器人 —— 主程序
==============================
基于 Selenium + Edge/Chrome 的淘宝商品抢购工具。

功能：
  - 智能浏览器管理：自动使用 Edge 用户数据目录，保留登录态
  - 反检测：隐藏 webdriver 特征，绕过淘宝自动化检测
  - 精准定时：毫秒级倒计时，提前选好规格
  - 自动选规格：大小写不敏感，精确匹配避免误选套装
  - 自动下单：购买 → 确认订单 → 提交支付
  - 关键截图：订单确认页、支付页自动截图存证
  - 登录态保护：不暴力杀进程，提示手动关闭浏览器

用法:
  python bot.py login          # 手动登录，保存Cookie
  python bot.py run            # 抢购主流程
  python bot.py dry-run        # 预检（不下单）
  python bot.py run -c my.yaml # 指定配置
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from selenium.webdriver.common.by import By
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    ElementClickInterceptedException,
    StaleElementReferenceException,
    WebDriverException,
)

from config_loader import load_config

# ===========================================================================
# 日志
# ===========================================================================

logger = logging.getLogger("taobao_grab")


def _setup_logging(log_dir: str = "logs") -> None:
    """配置日志：同时输出到终端和文件。"""
    if logger.handlers:
        return  # 防止重复添加 handler
    logger.setLevel(logging.DEBUG)

    # 终端输出
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(console)

    # 文件输出
    log_path = Path(log_dir)
    log_path.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fh = logging.FileHandler(log_path / f"grab_{ts}.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)


def _info(msg: str) -> None:
    logger.info(msg)


def _warn(msg: str) -> None:
    logger.warning(msg)


def _err(msg: str) -> None:
    logger.error(msg)


# ===========================================================================
# 自定义异常
# ===========================================================================


class FatalError(Exception):
    """不可恢复的致命错误，不应重试。"""


class RecoverableError(Exception):
    """可恢复的错误，可以重试。"""


# ===========================================================================
# 浏览器自动检测
# ===========================================================================


def _detect_browser() -> str:
    """自动检测可用浏览器，优先 Edge（Windows 自带）。"""
    if sys.platform == "win32":
        edge_paths = [
            Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe",
        ]
        for p in edge_paths:
            if p.exists():
                return "edge"

    # macOS / Linux 检测 Chrome
    chrome_paths = [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for p in chrome_paths:
        if Path(p).exists():
            return "chrome"

    return "edge"  # 默认尝试 Edge


# ===========================================================================
# 配置验证
# ===========================================================================


def _validate_config(cfg: dict) -> None:
    """启动前验证关键配置项。"""
    errors = []

    product = cfg.get("product", {})
    if not product.get("url"):
        errors.append("product.url 不能为空，请编辑 config.yaml")
    elif not product["url"].startswith("http"):
        errors.append(f"product.url 格式异常: {product['url']}")

    start_time = product.get("start_time", "")
    if start_time:
        try:
            datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            errors.append(f"product.start_time 格式错误，应为 YYYY-MM-DD HH:MM:SS，实际: {start_time}")

    max_retries = cfg.get("max_retries", 0)
    if not isinstance(max_retries, int) or max_retries < 1:
        errors.append(f"max_retries 应为正整数，实际: {max_retries}")

    if errors:
        for e in errors:
            _err(f"配置错误: {e}")
        raise FatalError("配置验证失败，请检查 config.yaml")


# ===========================================================================
# 核心抢购类
# ===========================================================================


class TaobaoGrabber:

    """封装浏览器生命周期与抢购流程。"""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.driver = None
        self.wait = None
        self._browser_type: str = ""
        self._headless: bool = cfg.get("headless", False)
        self._using_user_data_dir: bool = False  # 是否实际使用了用户数据目录

        # 注册信号处理，确保浏览器被正确关闭
        self._original_sigint = signal.getsignal(signal.SIGINT)
        self._original_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """处理中断信号，清理浏览器。"""
        _warn(f"收到信号 {signum}，正在清理...")
        self._quit()
        # 恢复原始信号处理器并重新触发
        signal.signal(signal.SIGINT, self._original_sigint)
        signal.signal(signal.SIGTERM, self._original_sigterm)
        sys.exit(1)

    # ------------------------------------------------------------------
    # 浏览器管理
    # ------------------------------------------------------------------

    def _build_driver(self) -> None:
        """启动浏览器，自动选择 Chrome 或 Edge。"""
        browser_cfg = self.cfg.get("browser", {})
        browser_type = browser_cfg.get("type", "auto")

        if browser_type == "auto":
            browser_type = _detect_browser()
            _info(f"自动检测浏览器: {browser_type}")

        self._browser_type = browser_type

        if browser_type == "edge":
            self._build_edge(browser_cfg)
        else:
            self._build_chrome(browser_cfg)

    def _build_edge(self, browser_cfg: dict) -> None:
        """使用 Selenium 内置 Edge 驱动。"""
        from selenium.webdriver import Edge, EdgeOptions
        import tempfile

        # 公共选项
        def _make_opts():
            opts = EdgeOptions()
            opts.add_argument("--disable-blink-features=AutomationControlled")
            opts.add_experimental_option("excludeSwitches", ["enable-automation"])
            opts.add_experimental_option("useAutomationExtension", False)
            opts.add_argument("--no-first-run")
            opts.add_argument("--no-default-browser-check")
            opts.add_argument("--disable-background-networking")
            opts.add_argument("--disable-popup-blocking")
            opts.add_argument("--window-size=1366,768")
            opts.add_argument("--lang=zh-CN")
            if self._headless:
                opts.add_argument("--headless=new")
            return opts

        # 驱动路径（跨平台）
        from pathlib import Path as _P
        import os, platform
        _cache = _P(os.path.expanduser('~')) / '.cache' / 'selenium' / 'msedgedriver'
        if _cache.exists():
            # 自动匹配架构：win64 / arm64 / mac-arm64 / mac-x64
            _arch = 'win64' if platform.machine().endswith('64') else 'arm64'
            if sys.platform == 'darwin':
                _arch = 'mac-arm64' if platform.machine() == 'arm64' else 'mac-x64'
            _versions = sorted(_cache.glob(f'{_arch}/*'), reverse=True)
            _exe = 'msedgedriver.exe' if sys.platform == 'win32' else 'msedgedriver'
            _driver_path = str(_versions[0] / _exe) if _versions and _versions[0].exists() else None
        else:
            _driver_path = None

        def _start_edge(opts):
            if _driver_path and _P(_driver_path).exists():
                return Edge(options=opts, service=EdgeService(_driver_path))
            return Edge(options=opts)

        profile = browser_cfg.get("user_data_dir", "")

        if profile:
            # 先尝试用 user_data_dir
            opts = _make_opts()
            opts.add_argument(f"--user-data-dir={profile}")
            try:
                _info(f"尝试使用 Edge 配置目录: {profile}")
                self.driver = _start_edge(opts)
                self._using_user_data_dir = True
                _info("成功使用 Edge 配置目录")
            except Exception as e:
                # 被锁定 → 自动关闭 Edge 后台进程
                _warn(f"配置目录被锁定，正在关闭 Edge 后台进程...")
                import subprocess
                subprocess.run(["taskkill", "/F", "/IM", "msedge.exe"],
                               capture_output=True, timeout=5)
                import time as _t
                _t.sleep(2)
                try:
                    opts = _make_opts()
                    opts.add_argument(f"--user-data-dir={profile}")
                    self.driver = _start_edge(opts)
                    self._using_user_data_dir = True
                    _info("关闭 Edge 后成功使用配置目录")
                except Exception as e2:
                    # 彻底失败 → 回退到临时目录
                    _warn(f"仍无法使用配置目录，回退到临时目录: {e2}")
                    opts = _make_opts()
                    tmp_dir = tempfile.mkdtemp(prefix="taobao_grab_")
                    opts.add_argument(f"--user-data-dir={tmp_dir}")
                    self.driver = _start_edge(opts)
                    self._using_user_data_dir = False
                    _info("使用临时配置目录（Cookie 文件方式）")
        else:
            opts = _make_opts()
            tmp_dir = tempfile.mkdtemp(prefix="taobao_grab_")
            opts.add_argument(f"--user-data-dir={tmp_dir}")
            self.driver = _start_edge(opts)
            self._using_user_data_dir = False
            _info("使用临时配置目录")

        self.driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
        )
        self.driver.implicitly_wait(1)
        self.wait = WebDriverWait(self.driver, 5)
        _info("Edge 浏览器已启动")

    def _build_chrome(self, browser_cfg: dict) -> None:
        """使用 undetected-chromedriver 启动 Chrome。"""
        import undetected_chromedriver as uc

        opts = uc.ChromeOptions()

        profile = browser_cfg.get("user_data_dir", "")
        if profile:
            opts.add_argument(f"--user-data-dir={profile}")
            self._using_user_data_dir = True

        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-infobars")
        opts.add_argument("--disable-popup-blocking")
        opts.add_argument("--window-size=1366,768")
        opts.add_argument("--lang=zh-CN")

        if self._headless:
            opts.add_argument("--headless=new")

        self.driver = uc.Chrome(options=opts, use_subprocess=True)
        self.driver.implicitly_wait(3)
        self.wait = WebDriverWait(self.driver, 10)
        _info("Chrome 浏览器已启动")

    def _quit(self) -> None:
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None

    # ------------------------------------------------------------------
    # 元素操作
    # ------------------------------------------------------------------

    def _find(self, by: str, value: str, timeout: float = 1):
        try:
            return WebDriverWait(self.driver, timeout).until(
                EC.element_to_be_clickable((by, value))
            )
        except (TimeoutException, WebDriverException):
            return None

    def _click_text(self, texts: List[str], timeout: float = 0.5) -> bool:
        for txt in texts:
            xpath = f'//*[contains(normalize-space(text()),"{txt}")]'
            el = self._find(By.XPATH, xpath, timeout=timeout)
            if not el:
                continue
            try:
                el.click()
                return True
            except ElementClickInterceptedException:
                self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                self.driver.execute_script("arguments[0].click();", el)
                return True
            except StaleElementReferenceException:
                continue
        return False

    def _click_css(self, selectors: List[str], timeout: float = 0.3) -> bool:
        for sel in selectors:
            el = self._find(By.CSS_SELECTOR, sel, timeout=timeout)
            if el:
                try:
                    el.click()
                    return True
                except Exception:
                    self.driver.execute_script("arguments[0].click();", el)
                    return True
        return False

    # ------------------------------------------------------------------
    # SKU 选择
    # ------------------------------------------------------------------

    def _select_sku(self) -> None:
        keywords = self.cfg["product"].get("sku_keywords", [])
        if not keywords:
            return

        # 一次 JS 调用处理所有关键词，避免多次 execute_script 开销（大小写不敏感）
        # 转义双引号防止 XPath 注入
        kw_list = json.dumps([str(k).strip().replace('"', '\\"') for k in keywords if str(k).strip()])
        js_all = f"""
        var keywords = {kw_list};
        var clicked = [];
        for (var k = 0; k < keywords.length; k++) {{
            var kw = keywords[k];
            var kwLower = kw.toLowerCase();
            var found = false;

            // 1. 精确匹配 title 属性（大小写不敏感）
            var els = document.querySelectorAll('li[title]');
            for (var i = 0; i < els.length; i++) {{
                if (els[i].title.toLowerCase() === kwLower) {{ els[i].click(); found = true; break; }}
            }}
            if (found) {{ clicked.push(kw); continue; }}

            // 2. 遍历找文字精确匹配（大小写不敏感），跳过组合选项
            var candidates = document.querySelectorAll('li, span, div, a, button');
            for (var i = 0; i < candidates.length; i++) {{
                var c = candidates[i];
                var text = c.textContent.trim();
                if (text.toLowerCase() === kwLower) {{ c.click(); found = true; break; }}
                if (text.toLowerCase().indexOf(kwLower) === 0 && text.length > kw.length) {{
                    var next = text.charAt(kw.length);
                    if (next === '+' || next === ' ' || next === '（' || next === '(') continue;
                }}
            }}
            if (found) {{ clicked.push(kw); continue; }}

            // 3. 兜底 contains
            var xpath = '//*[contains(@title,"' + kw + '") or contains(text(),"' + kw + '")]';
            var result = document.evaluate(xpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
            if (result.singleNodeValue) {{ result.singleNodeValue.click(); clicked.push(kw); }}
        }}
        return clicked;
        """
        clicked = self.driver.execute_script(js_all)
        for kw in clicked:
            _info(f"已选规格: {kw}")
        not_found = [str(k).strip() for k in keywords if str(k).strip() and str(k).strip() not in clicked]
        for kw in not_found:
            _warn(f"未找到规格: {kw}")

    # ------------------------------------------------------------------
    # 验证码检测
    # ------------------------------------------------------------------

    def _has_captcha(self) -> bool:
        indicators = [
            "//iframe[contains(@src,'captcha')]",
            "//*[contains(text(),'请完成验证')]",
            "//*[contains(text(),'滑动验证')]",
            "//*[contains(text(),'安全验证')]",
            "//*[contains(@class,'nc-container')]",
            "//*[contains(@class,'captcha')]",
            "//div[@id='nocaptcha']",
            "//div[@id='J_Captcha']",
        ]
        for xp in indicators:
            try:
                elements = self.driver.find_elements(By.XPATH, xp)
                if elements:
                    return True
            except NoSuchElementException:
                continue
        return False

    def _wait_captcha(self) -> None:
        if not self._has_captcha():
            return

        if self._headless:
            raise FatalError(
                "检测到验证码但当前为 headless 模式，无法手动验证！\n"
                "请设置 headless: false 后重试，或先手动登录保存 Cookie。"
            )

        print()
        print("=" * 52)
        print("  !! 检测到验证码 / 安全验证 !!")
        print("  请在浏览器窗口中手动完成")
        print("  完成后回到此终端按 Enter 继续")
        print("=" * 52)
        input(">>> 按 Enter ...")

    # ------------------------------------------------------------------
    # 截图
    # ------------------------------------------------------------------

    def _screenshot(self, tag: str) -> None:
        try:
            ss_dir = Path("screenshots")
            ss_dir.mkdir(exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = ss_dir / f"{tag}_{ts}.png"
            self.driver.save_screenshot(str(path))
            _info(f"截图 -> {path}")
        except Exception as e:
            _warn(f"截图失败: {e}")

    # ------------------------------------------------------------------
    # 倒计时
    # ------------------------------------------------------------------

    @staticmethod
    def _countdown(target_str: str) -> None:
        if not target_str:
            return

        from datetime import timezone, timedelta
        target = datetime.strptime(target_str, "%Y-%m-%d %H:%M:%S")
        # 假设用户配置的是本地时间（中国 UTC+8），转为 aware datetime
        local_tz = timezone(timedelta(hours=8))
        target = target.replace(tzinfo=local_tz)
        _info(f"目标时间: {target}")

        while True:
            from datetime import timezone, timedelta
            local_tz = timezone(timedelta(hours=8))
            now = datetime.now(tz=local_tz)
            remaining = (target - now).total_seconds()
            if remaining <= 0:
                break
            if remaining > 2:
                m, s = divmod(int(remaining), 60)
                print(f"\r  [倒计时] {m:02d}:{s:02d} ", end="", flush=True)
                time.sleep(0.5)
            else:
                # 最后 2 秒：用短 sleep 代替 busy-wait，降低 CPU 占用
                while (target - datetime.now(tz=local_tz)).total_seconds() > 0.001:
                    time.sleep(0.001)
                break

        print("\r  [!!] 时间到！开始抢购！              ")

    # ------------------------------------------------------------------
    # 登录流程
    # ------------------------------------------------------------------

    def login(self) -> None:
        self._build_driver()
        try:
            self.driver.get(self.cfg["login"]["login_url"])
            print()
            print("=" * 52)
            print("  请在浏览器中手动登录淘宝")
            print("  （扫码 / 账密均可）")
            print("  登录成功后回到终端按 Enter")
            print("=" * 52)
            input(">>> 按 Enter ...")

            cookie_path = Path(self.cfg["login"]["cookie_path"])
            cookie_path.parent.mkdir(parents=True, exist_ok=True)
            with cookie_path.open("w", encoding="utf-8") as f:
                json.dump(self.driver.get_cookies(), f, ensure_ascii=False, indent=2)
            _info(f"Cookies 已保存 -> {cookie_path}")

            profile = self.cfg["browser"].get("user_data_dir", "")
            if profile:
                _info(f"Chrome/Edge 配置目录 -> {profile}（登录态自动持久化）")

            self._screenshot("login_ok")
        finally:
            self._quit()

    # ------------------------------------------------------------------
    # 加载 Cookie
    # ------------------------------------------------------------------

    def _load_cookies(self) -> None:
        """从文件加载 Cookie（兜底方案，优先靠 user_data_dir）。"""
        cookie_path = Path(self.cfg["login"]["cookie_path"])
        if not cookie_path.exists():
            return

        # 访问淘宝域名，确保能加载 cookie
        self.driver.get(self.cfg["login"]["login_url"])
        try:
            WebDriverWait(self.driver, 3).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except Exception:
            pass

        with cookie_path.open("r", encoding="utf-8") as f:
            cookies = json.load(f)

        for c in cookies:
            if "domain" not in c:
                c["domain"] = ".taobao.com"
            try:
                self.driver.add_cookie(c)
            except Exception:
                # domain 不匹配时尝试当前域名
                try:
                    c["domain"] = ".tmall.com"
                    self.driver.add_cookie(c)
                except Exception as e:
                    _warn(f"添加 cookie 失败: {e}")

        _info("已加载 Cookie 文件")

    # ------------------------------------------------------------------
    # 抢购主流程
    # ------------------------------------------------------------------

    def grab(self, dry_run: bool = False) -> None:
        self._build_driver()
        try:
            # 加载 Cookie（仅当未实际使用用户数据目录时）
            if not self._using_user_data_dir:
                self._load_cookies()

            # 打开商品页
            self.driver.get(self.cfg["product"]["url"])
            try:
                WebDriverWait(self.driver, 3).until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )
            except Exception:
                pass
            _info(f"已打开商品页 -> {self.driver.current_url}")

            # 如果被 TMD 安全验证拦截，等它自动跳回（不用刷新）
            cur = self.driver.current_url
            if "tmd" in cur or "login_jump" in cur:
                _info("经过 TMD 安全验证，等待跳转...")
                try:
                    WebDriverWait(self.driver, 5).until(
                        lambda d: "tmd" not in d.current_url and "login_jump" not in d.current_url
                    )
                except Exception:
                    pass
                _info(f"验证后页面 -> {self.driver.current_url}")

            # 检测登录页，提示用户重新登录
            cur_url = self.driver.current_url
            if "login.taobao.com" in cur_url or "login.htm" in cur_url.split("?")[0]:
                if self._headless:
                    raise FatalError("登录态已过期，请先运行 python bot.py login 登录")
                _warn("登录态已过期！")
                print()
                print("=" * 52)
                print("  登录态已过期，请在浏览器中重新登录")
                print("  登录完成后回到终端按 Enter 继续")
                print("=" * 52)
                input(">>> 按 Enter ...")
                # 登录后重新打开商品页
                self.driver.get(self.cfg["product"]["url"])
                try:
                    WebDriverWait(self.driver, 5).until(
                        lambda d: d.execute_script("return document.readyState") == "complete"
                    )
                except Exception:
                    pass
                _info(f"重新打开商品页 -> {self.driver.current_url}")

            # 提前选好规格（倒计时前就选好，开抢时直接点购买）
            self._select_sku()

            # 等待开抢
            self._countdown(self.cfg["product"]["start_time"])

            # 重试抢购
            last_err: Optional[Exception] = None
            max_retry = self.cfg["max_retries"]

            for attempt in range(1, max_retry + 1):
                try:
                    _info(f"===== 第 {attempt}/{max_retry} 次尝试 =====")

                    if attempt > 1:
                        self.driver.refresh()
                        # 等页面就绪再继续
                        try:
                            WebDriverWait(self.driver, 5).until(
                                lambda d: d.execute_script("return document.readyState") == "complete"
                            )
                        except Exception:
                            pass
                        # 刷新后重新选规格
                        self._select_sku()

                    # 点购买 - 一次 JS 调用遍历所有关键词
                    buy_keywords = self.cfg["keywords"]["buy_now"]
                    kw_json = json.dumps(buy_keywords)
                    js_buy = f"""
                    var keywords = {kw_json};
                    for (var k = 0; k < keywords.length; k++) {{
                        var xpath = '//*[contains(text(),"' + keywords[k] + '")]';
                        var result = document.evaluate(xpath, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
                        for (var i = 0; i < result.snapshotLength; i++) {{
                            var el = result.snapshotItem(i);
                            var style = window.getComputedStyle(el);
                            var rect = el.getBoundingClientRect();
                            if (style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0'
                                && rect.width > 0 && rect.height > 0
                                && (el.tagName === 'A' || el.tagName === 'BUTTON' || el.tagName === 'DIV' || el.tagName === 'SPAN' || el.tagName === 'INPUT')) {{
                                if (el.disabled || el.classList.contains('disabled')) continue;
                                el.click();
                                return keywords[k];
                            }}
                        }}
                    }}
                    // CSS 兜底
                    var cssSelectors = {json.dumps(self.cfg["selector"]["buy_button_selectors"])};
                    for (var s = 0; s < cssSelectors.length; s++) {{
                        var el = document.querySelector(cssSelectors[s]);
                        if (el && el.offsetParent !== null) {{ el.click(); return 'CSS:' + cssSelectors[s]; }}
                    }}
                    return false;
                    """
                    bought = self.driver.execute_script(js_buy)
                    if bought:
                        _info(f"已点击购买按钮: {bought}")
                    else:
                        _warn(f"当前页面: {self.driver.current_url}")
                        _warn(f"页面标题: {self.driver.title}")
                        raise RecoverableError("未找到购买按钮（页面未就绪或已改版）")

                    if dry_run:
                        time.sleep(1)  # 等待页面跳转
                        _info(f"DRY-RUN 完成：购买入口已找到，跳过下单")
                        _info(f"点击后页面: {self.driver.current_url}")
                        self._screenshot("dry_run")
                        return

                    # ---- 等待订单确认页加载 ----
                    _info("等待订单确认页加载...")
                    cur_url = self.driver.current_url
                    if "confirm" in cur_url or "order" in cur_url or "buy" in cur_url:
                        _info("已在订单确认页")
                    else:
                        for _wait in range(30):  # 最多等 3 秒
                            time.sleep(0.1)
                            cur_url = self.driver.current_url
                            if "confirm" in cur_url or "order" in cur_url or "buy" in cur_url:
                                break
                        else:
                            _warn(f"页面未跳转到订单确认页: {self.driver.current_url}")

                    # 检查是否跳转到了错误页面
                    cur_url = self.driver.current_url
                    if "list_bought_items" in cur_url or "itemlist" in cur_url:
                        _warn("跳转到了订单列表页，可能商品已售罄或未到开抢时间")
                        self._screenshot("wrong_page")
                        raise RecoverableError("商品可能已售罄或未到开抢时间")

                    # 等订单确认页加载完再截图
                    try:
                        WebDriverWait(self.driver, 5).until(
                            lambda d: d.execute_script("return document.readyState") == "complete"
                        )
                    except Exception:
                        pass
                    time.sleep(1)
                    self._screenshot("confirm_order")

                    # ---- 提交订单（一次 JS 搜索，快速） ----
                    if self.cfg["cart"]["auto_checkout"]:
                        confirm_kw_json = json.dumps(self.cfg["keywords"]["confirm"])
                        js_submit = f"""
                        var keywords = {confirm_kw_json};
                        for (var k = 0; k < keywords.length; k++) {{
                            var xpath = '//*[contains(text(),"' + keywords[k] + '")]';
                            var result = document.evaluate(xpath, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
                            for (var i = 0; i < result.snapshotLength; i++) {{
                                var el = result.snapshotItem(i);
                                var style = window.getComputedStyle(el);
                                var rect = el.getBoundingClientRect();
                                if (style.display !== 'none' && style.visibility !== 'hidden'
                                    && rect.width > 0 && rect.height > 0
                                    && !el.disabled) {{
                                    el.click();
                                    return keywords[k];
                                }}
                            }}
                        }}
                        return false;
                        """
                        submitted = self.driver.execute_script(js_submit)
                        if submitted:
                            _info(f"[OK] 已点击: {submitted}")
                        else:
                            _warn("未找到提交订单按钮，请手动完成")

                    # 等待跳转到支付页（最多 10 秒）
                    _info("等待跳转...")
                    for _w in range(100):
                        time.sleep(0.1)
                        cur = self.driver.current_url
                        if "cashier" in cur or "pay" in cur or "alipay" in cur:
                            _info(f"[OK] 已跳转到支付页面！")
                            break
                    else:
                        _warn(f"未跳转到支付页，当前: {self.driver.current_url}")

                    # 等支付页面加载完再截图
                    try:
                        WebDriverWait(self.driver, 5).until(
                            lambda d: d.execute_script("return document.readyState") == "complete"
                        )
                    except Exception:
                        pass
                    time.sleep(1)
                    self._screenshot("payment")

                    print()
                    print("=" * 52)
                    print("  请在浏览器中完成支付")
                    print("  完成后回到终端按 Enter 关闭")
                    print("=" * 52)
                    input(">>> 按 Enter 关闭浏览器 ...")
                    return
                except FatalError:
                    # 致命错误，直接抛出不重试
                    raise
                except Exception as exc:
                    last_err = exc
                    _warn(f"第 {attempt} 次失败: {exc}")
                    self._screenshot(f"err_{attempt}")
                    if attempt < max_retry:
                        time.sleep(self.cfg["retry_wait_seconds"])

            _err(f"已重试 {max_retry} 次仍未成功")
            if last_err:
                raise last_err
        finally:
            self._quit()


# ===========================================================================
# CLI
# ===========================================================================


def main() -> None:
    parser = argparse.ArgumentParser(description="淘宝抢购脚本")
    parser.add_argument("command", choices=["login", "run", "dry-run"], help="执行命令")
    parser.add_argument("-c", "--config", default="config.yaml", help="配置文件路径")
    args = parser.parse_args()

    _setup_logging()
    _info("淘宝抢购脚本启动")

    try:
        cfg = load_config(args.config)
        _validate_config(cfg)
    except FileNotFoundError as e:
        _err(str(e))
        sys.exit(1)
    except FatalError:
        sys.exit(1)

    grabber = TaobaoGrabber(cfg)

    if args.command == "login":
        grabber.login()
    elif args.command == "run":
        grabber.grab(dry_run=False)
    elif args.command == "dry-run":
        grabber.grab(dry_run=True)
    else:
        _err(f"未知命令: {args.command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
