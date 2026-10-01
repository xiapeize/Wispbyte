#!/usr/bin/env python3

import os
import sys
import time
import logging
import tempfile
import subprocess
from pathlib import Path
from datetime import datetime
from typing import List, Tuple, Optional

import requests
from seleniumbase import SB
from seleniumbase.common.exceptions import TimeoutException

# ====================== 配置 ======================
LOGIN_URL = "https://wispbyte.com/client"
DASHBOARD_URL = "https://wispbyte.com/client/dashboard"
CONSOLE_URL_TEMPLATE = "https://wispbyte.com/client/servers/{identifier}/console"

WORKSPACE = os.environ.get("GITHUB_WORKSPACE", str(Path.cwd()))
OUTPUT_DIR = Path(WORKSPACE) / "output/screenshots"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("wispbyte_restart")

for _noisy in ("seleniumbase", "selenium", "urllib3", "undetected_chromedriver"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)


# ====================== 工具函数 ======================
def mask_email(email: str) -> str:
    if '@' not in email:
        return email[:1] + "***"
    local, domain = email.split('@', 1)
    masked_local = local[:1] + "***" if local else "***"
    if '.' in domain:
        parts = domain.split('.')
        tld = parts[-1]
        first_char = domain[0]
        masked_domain = f"{first_char}***.{tld}"
    else:
        masked_domain = domain[:1] + "***"
    return f"{masked_local}@{masked_domain}"


def mask_server_id(identifier: str) -> str:
    if not identifier:
        return "***"
    if len(identifier) <= 4:
        return "***"
    return identifier[:2] + "***" + identifier[-2:]


def log(msg: str, level: str = "INFO"):
    prefix = {"INFO": "[INFO]", "WARN": "[WARN]", "ERROR": "[ERROR]"}.get(level, "[INFO]")
    logger.info(f"{prefix} {msg}")


def send_tg_photo(token: str, chat_id: str, photo_path: str, caption: str):
    if not token or not chat_id:
        return
    if not photo_path or not os.path.exists(photo_path):
        log(f"截图文件不存在: {photo_path}", "WARN")
        return
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    try:
        with open(photo_path, "rb") as f:
            resp = requests.post(
                url,
                data={"chat_id": chat_id, "caption": caption},
                files={"photo": f},
                timeout=30
            )
        resp.raise_for_status()
        log("Telegram 图片通知发送成功")
    except Exception as e:
        log(f"Telegram 通知异常: {e}", "ERROR")


def restart_warp():
    log("正在重启 WARP 以更换 IP...")
    try:
        old_ip = requests.get("https://api.ipify.org", timeout=10).text
        log(f"当前 IP: {old_ip}")
    except Exception:
        old_ip = "未知"
    try:
        subprocess.run(["sudo", "warp-cli", "--accept-tos", "disconnect"],
                       check=False, timeout=30, capture_output=True)
        time.sleep(3)
        try:
            subprocess.run(["sudo", "warp-cli", "--accept-tos", "registration", "delete"],
                           check=True, timeout=30, capture_output=True)
        except subprocess.CalledProcessError:
            log("删除注册失败（可能未注册），继续...", "WARN")
        subprocess.run(["sudo", "warp-cli", "--accept-tos", "registration", "new"],
                       check=True, timeout=30, capture_output=True)
        time.sleep(3)
        subprocess.run(["sudo", "warp-cli", "--accept-tos", "connect"],
                       check=True, timeout=30, capture_output=True)
        time.sleep(10)
        new_ip = requests.get("https://api.ipify.org", timeout=10).text
        log(f"WARP 重连成功，新 IP: {new_ip}")
        return True
    except Exception as e:
        log(f"WARP 重连失败: {e}", "ERROR")
        return False


def take_screenshot(sb, account_index: int, suffix: str) -> str:
    timestamp = datetime.now().strftime("%H%M%S")
    filename = f"acc{account_index}-{suffix}-{timestamp}.png"
    filepath = str(OUTPUT_DIR / filename)
    try:
        sb.save_screenshot(filepath)
        log(f"📸 截图保存: {filepath}")
        return filepath
    except Exception as e:
        log(f"截图失败: {e}", "WARN")
        return ""


# ====================== Turnstile 处理 ======================
def check_turnstile_solved(sb) -> bool:
    """检查当前页面/弹窗中的 Turnstile 是否已完成"""
    try:
        return bool(sb.execute_script('''
            var inp = document.querySelector('input[name="cf-turnstile-response"]');
            if (inp && inp.value && inp.value.length > 20) return true;
            var iframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
            if (iframe && iframe.getAttribute("data-state") === "solved") return true;
            var success = document.getElementById('success');
            return !!(success && getComputedStyle(success).display !== 'none');
        '''))
    except Exception:
        return False


def wait_for_turnstile_success(sb, timeout: int = 30) -> bool:
    """等待并点击登录页 Turnstile"""
    log("等待 Turnstile 验证...")
    start = time.time()
    last_click = 0
    while time.time() - start < timeout:
        if check_turnstile_solved(sb):
            log("✅ Turnstile 验证成功")
            return True
        if time.time() - last_click > 3:
            try:
                sb.uc_gui_click_captcha()
                last_click = time.time()
                log("点击 Turnstile")
            except Exception as e:
                log(f"点击 Turnstile 异常: {e}", "WARN")
        time.sleep(1)
    log("⏰ Turnstile 验证超时", "WARN")
    return False


# ====================== 登录流程 ======================
def login(sb, email: str, password: str) -> bool:
    log("访问登录页...")
    sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=10)
    time.sleep(4)

    try:
        sb.wait_for_element_visible('input#email', timeout=15)
        log("✅ 找到登录表单")
    except TimeoutException:
        log("未找到登录表单，尝试重新连接...", "WARN")
        sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=10)
        time.sleep(5)
        try:
            sb.wait_for_element_visible('input#email', timeout=10)
        except TimeoutException:
            log("仍然未找到登录表单", "ERROR")
            return False

    log("填写登录信息...")
    sb.type('input#email', email)
    time.sleep(0.5)
    sb.type('input#password', password)
    time.sleep(0.5)

    if not wait_for_turnstile_success(sb, timeout=35):
        log("登录 Turnstile 未通过", "ERROR")
        return False

    log("提交登录...")
    try:
        sb.click('button.login-btn')
    except Exception:
        sb.execute_script('document.querySelector("form#login-form").submit()')

    log("等待跳转到仪表盘...")
    for _ in range(15):
        if "/dashboard" in sb.get_current_url() or "/client/dashboard" in sb.get_current_url():
            log("已跳转到仪表盘")
            break
        time.sleep(1)
    else:
        log("登录后未成功跳转到仪表盘", "ERROR")
        return False

    DASHBOARD_SELECTORS = [
        'div.server-list', 'div.servers-container', 'div.card',
        'div.server-card', 'table', 'main', 'section', '#app',
    ]
    dashboard_ready = False
    for sel in DASHBOARD_SELECTORS:
        try:
            sb.wait_for_element_present(sel, timeout=3)
            dashboard_ready = True
            log(f"✅ 仪表盘已就绪 ({sel})")
            break
        except Exception:
            continue

    if not dashboard_ready:
        try:
            body_len = sb.execute_script("return document.body.innerText.length")
            if body_len and int(body_len) > 100:
                log("✅ 仪表盘页面有内容，继续执行")
                dashboard_ready = True
        except Exception:
            pass

    if not dashboard_ready:
        log("仪表盘结构未识别，但继续执行", "WARN")

    log("✅ 登录成功并进入仪表盘")
    return True


# ====================== 获取服务器列表 ======================
def get_servers(sb) -> List[str]:
    log("通过 fetch 请求服务器列表...")
    try:
        result = sb.execute_async_script('''
            var callback = arguments[arguments.length - 1];
            fetch('/client/api/servers/status', {
                method: 'GET',
                headers: { 'Accept': 'application/json' }
            })
            .then(function(res) { return res.json(); })
            .then(function(data) {
                if (data.servers) {
                    callback(data.servers.map(function(s) { return s.identifier; }));
                } else {
                    callback([]);
                }
            })
            .catch(function(err) { callback([]); });
        ''')
        if result and isinstance(result, list):
            ids = [str(i) for i in result if i]
            if ids:
                masked = [mask_server_id(i) for i in ids]
                log(f"成功获取服务器列表，共 {len(ids)} 台: {masked}")
                return ids
    except Exception as e:
        log(f"fetch 请求失败: {e}", "ERROR")

    # 备用 DOM 提取
    try:
        dom_ids = sb.execute_script('''
            var cards = document.querySelectorAll(
                '[data-server-id], .server-card, .server-item'
            );
            return Array.from(cards).map(function(el) {
                return el.getAttribute('data-server-id') || el.id;
            }).filter(Boolean);
        ''')
        if dom_ids:
            masked = [mask_server_id(i) for i in dom_ids]
            log(f"从 DOM 提取到服务器，共 {len(dom_ids)} 台: {masked}")
            return list(dom_ids)
    except Exception as e:
        log(f"DOM 提取失败: {e}", "WARN")

    log("未能获取任何服务器标识符", "ERROR")
    return []


# ====================== 访问服务器控制台 ======================
def visit_console(sb, identifier: str) -> bool:
    """
    仅导航到服务器控制台页面，不做任何操作。
    """
    console_url = CONSOLE_URL_TEMPLATE.format(identifier=identifier)
    safe_id = mask_server_id(identifier)
    log(f"{'─'*40}")
    log(f"访问控制台: {safe_id}")
    log(f"{'─'*40}")

    log(f"导航到控制台: {safe_id}")
    sb.get(console_url)
    time.sleep(5)
    log(f"✅ 已进入控制台: {safe_id}")
    return True


# ====================== 账号处理 ======================
def process_account(idx: int, email: str, password: str, tg_token: str, tg_chat: str):
    log(f"{'='*50}")
    log(f"开始处理账号 {idx} | {mask_email(email)}")
    log(f"{'='*50}")

    user_data_dir = tempfile.mkdtemp(prefix=f"wisp_usr_{idx}_")
    with SB(uc=True, test=True, locale="en", headed=False,
            user_data_dir=user_data_dir,
            chromium_arg="--disable-blink-features=AutomationControlled") as sb:
        try:
            if not login(sb, email, password):
                screenshot = take_screenshot(sb, idx, "login-fail")
                send_tg_photo(tg_token, tg_chat, screenshot,
                              f"❌ 登录失败\n账号: {mask_email(email)}\n\nWispbyte Auto Restart")
                return

            servers = get_servers(sb)
            if not servers:
                screenshot = take_screenshot(sb, idx, "no-server")
                send_tg_photo(tg_token, tg_chat, screenshot,
                              f"❌ 未找到服务器\n账号: {mask_email(email)}\n\nWispbyte Auto Restart")
                return

            for si, server_id in enumerate(servers, start=1):
                success = visit_console(sb, server_id)
                suffix = f"console-{si}" if len(servers) > 1 else "console"
                screenshot = take_screenshot(sb, idx, suffix)
                status_icon = "✅" if success else "❌"
                status_text = "已进入控制台" if success else "访问失败"
                caption = (
                    f"{status_icon} {status_text}\n\n"
                    f"账号: {mask_email(email)}\n"
                    f"服务器: {server_id}\n\n"
                    f"Wispbyte Auto Restart"
                )
                send_tg_photo(tg_token, tg_chat, screenshot, caption)

        except Exception as e:
            log(f"账号 {idx} 处理异常: {e}", "ERROR")
            screenshot = take_screenshot(sb, idx, "exception")
            send_tg_photo(tg_token, tg_chat, screenshot,
                          f"❌ 脚本异常\n账号: {mask_email(email)}\n信息: {str(e)[:200]}\n\nWispbyte Auto Restart")


# ====================== 账号加载 ======================
def load_accounts() -> List[Tuple[str, str]]:
    accounts = []
    for i in range(1, 6):
        raw = os.environ.get(f"WISPBYTE_{i}")
        if not raw:
            continue
        parts = raw.split("-----")
        if len(parts) >= 2:
            email = parts[0].strip()
            password = parts[1].strip()
            if email and password:
                accounts.append((email, password))
                log(f"加载账号 WISPBYTE_{i}: {mask_email(email)}")
            else:
                log(f"WISPBYTE_{i} 格式不正确（邮箱或密码为空）", "WARN")
        else:
            log(f"WISPBYTE_{i} 格式错误，期望 '邮箱-----密码'", "WARN")
    return accounts


def parse_target_emails(raw: str) -> List[str]:
    if not raw or not raw.strip():
        return []
    seen = set()
    result = []
    for part in raw.split(","):
        email = part.strip().lower()
        if not email:
            continue
        if "@" not in email:
            log(f"无效的邮箱格式: '{email}'，已跳过", "WARN")
            continue
        if email in seen:
            log(f"重复邮箱: '{email}'，已跳过", "WARN")
            continue
        seen.add(email)
        result.append(email)
    return result


# ====================== 入口 ======================
def main():
    tg_token = os.environ.get("TG_BOT_TOKEN", "").strip()
    tg_chat = os.environ.get("TG_CHAT_ID", "").strip()
    if not tg_token or not tg_chat:
        log("缺少 TG_BOT_TOKEN 或 TG_CHAT_ID，通知功能将不可用", "WARN")

    all_accounts = load_accounts()
    if not all_accounts:
        log("未找到任何有效账号，请检查 Secrets 设置", "ERROR")
        sys.exit(1)

    target_raw = os.environ.get("INPUT_ACCOUNTS", "").strip()
    target_emails = parse_target_emails(target_raw)

    if target_emails:
        all_email_map = {
            email.lower(): (idx, email, password)
            for idx, (email, password) in enumerate(all_accounts, start=1)
        }
        selected = []
        for target in target_emails:
            if target in all_email_map:
                selected.append(all_email_map[target])
            else:
                log(f"邮箱 '{mask_email(target)}' 未在已配置账号中找到，已跳过", "WARN")
        if not selected:
            log("指定的邮箱全部无效，退出", "ERROR")
            sys.exit(1)
        log(f"指定运行账号: {[mask_email(e) for _, e, _ in selected]}")
    else:
        selected = [(idx, email, password)
                    for idx, (email, password) in enumerate(all_accounts, start=1)]
        log("未指定账号，运行全部账号")

    for run_order, (idx, email, password) in enumerate(selected):
        if run_order > 0:
            restart_warp()
        process_account(idx, email, password, tg_token, tg_chat)
        if run_order < len(selected) - 1:
            time.sleep(5)

    log("所有账号处理完毕")


if __name__ == "__main__":
    main()
