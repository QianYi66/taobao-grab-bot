# 淘宝抢购脚本

基于 **Selenium + Edge/Chrome** 的淘宝/天猫商品抢购工具。

## ✨ 功能

- **智能浏览器管理**：自动使用 Edge 用户数据目录（保留登录态），被锁定时提示关闭浏览器
- **反检测**：隐藏 webdriver 特征，绕过淘宝自动化检测
- **精准定时**：毫秒级倒计时，最后 2 秒自旋保证精度
- **提前选规格**：倒计时前自动选好 SKU，开抢时直接点购买
- **自动选规格**：按关键词匹配颜色/尺码，大小写不敏感，精确匹配避免误选套装
- **自动下单**：点击购买 → 确认订单 → 提交支付
- **验证码暂停**：检测到滑块/验证码时自动暂停，等你手动处理
- **登录态保护**：不暴力杀进程，提示用户手动关闭浏览器
- **关键截图**：订单确认页、支付页自动截图存证
- **失败重试**：可配置重试次数，登录过期自动提示
- **预检模式**：dry-run 只检查元素，不下单

## 📁 项目结构

| 文件 | 说明 |
|------|------|
| `bot.py` | 主程序：浏览器管理、SKU 选择、购买下单、截图、重试等核心逻辑 |
| `config_loader.py` | 配置加载器：读取 config.yaml 并自动填充默认值 |
| `config.example.yaml` | 配置模板：复制为 config.yaml 后编辑 |
| `requirements.txt` | Python 依赖清单 |
| `LICENSE` | MIT 开源许可证 |
| `README.md` | 项目说明文档 |
| `.gitignore` | Git 忽略规则（不提交配置、截图、日志等） |

## 📦 环境要求

- Python 3.8+
- Microsoft Edge（已安装）
- 网络正常

## 🚀 快速开始

### 1. 安装依赖

```bash
cd taobao-grab-bot
pip install -r requirements.txt
```

### 2. 创建配置

```bash
cp config.example.yaml config.yaml
```

编辑 `config.yaml`，至少填写：

| 字段 | 说明 |
|------|------|
| `product.url` | 目标商品链接（只需商品 ID 部分即可） |
| `product.sku_keywords` | 规格关键词列表（如 `["咖啡色", "2xl"]`） |
| `product.start_time` | 开抢时间 `YYYY-MM-DD HH:MM:SS`（留空立即开始） |

### 3. 首次登录

```bash
python bot.py login
```

脚本会打开 Edge，你手动登录淘宝后回到终端按 Enter。

**推荐**：在 `config.yaml` 中设置 `browser.user_data_dir` 为 Edge 用户数据目录，登录态自动持久化。

| 系统 | 路径示例 |
|------|---------|
| Windows | `C:\Users\你的用户名\AppData\Local\Microsoft\Edge\User Data` |

> ⚠️ 使用 Edge 配置目录时，脚本会提示你关闭所有 Edge 窗口再运行。

### 4. 预检（推荐先跑一次）

```bash
python bot.py dry-run
```

确认页面可达、SKU 可选、购买按钮可点击。

### 5. 正式抢购

```bash
python bot.py run
```

## 📋 命令一览

```
python bot.py login              # 手动登录，保存 Cookie
python bot.py run                # 抢购
python bot.py dry-run            # 预检（不下单）
python bot.py run -c other.yaml  # 指定配置文件
```

## ⚙️ 配置说明

```yaml
headless: false            # 是否无头模式（调试时建议 false）
max_retries: 8             # 最大重试次数
retry_wait_seconds: 0.3    # 每次重试间隔（秒）

browser:
  type: auto               # 自动检测浏览器（edge/chrome）
  user_data_dir: ""        # Edge 用户数据目录（留空则用 Cookie 文件）

product:
  url: ""                  # 商品链接（必填，只需 id 参数即可）
  sku_keywords: []         # SKU 关键词（大小写不敏感）
  start_time: ""           # 开抢时间（留空立即开始）

cart:
  auto_checkout: true      # 是否自动提交订单

login:
  login_url: "https://www.taobao.com"
  cookie_path: "state/cookies.json"

keywords:
  buy_now:                 # 购买按钮关键词（按顺序匹配）
    - "领券购买"
    - "立即购买"
    - "马上抢"
  confirm:                 # 提交订单关键词
    - "立即支付"
    - "提交订单"
    - "同意协议并付款"

selector:
  buy_button_selectors:    # CSS 选择器兜底
    - "button.primary"
```

## 📸 截图说明

脚本会在关键步骤自动截图，保存在 `screenshots/` 目录：

| 文件名 | 说明 |
|--------|------|
| `confirm_order_*.png` | 订单确认页（点击提交前） |
| `payment_*.png` | 支付页面（点击提交后） |
| `err_*_*.png` | 失败时的页面截图 |
| `login_ok_*.png` | 登录成功截图 |

## 🔧 常见问题

**Q: 找不到购买按钮？**
A: 淘宝页面经常改版，更新 `keywords.buy_now` 和 `selector.buy_button_selectors`。用 `dry-run` 模式调试。

**Q: 被检测到自动化？**
A: 使用 `user_data_dir`（已默认），不要用 `headless: true`。

**Q: 验证码怎么办？**
A: 脚本会自动暂停，你手动完成验证后按 Enter 继续。

**Q: 要抢多个商品？**
A: 复制多个 config 文件，分别运行。

**Q: 到点抢购怎么配置？**
A: 设置 `product.start_time`，脚本会提前打开页面并选好规格，时间到直接点购买。

```yaml
product:
  url: "https://detail.tmall.com/item.htm?id=xxx"
  sku_keywords: ["咖啡色", "2xl"]
  start_time: "2026-06-26 10:00:00"
```

**Q: Edge 被锁定怎么办？**
A: 脚本会提示你关闭所有 Edge 窗口，按 Enter 后重试。不要用任务管理器强制关闭（会丢失登录态）。

## ⚠️ 免责声明

本脚本仅供学习和个人自动化使用。使用时请遵守相关网站服务条款。
