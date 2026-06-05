"""
微信阅读量直接获取模块
通过 Fiddler 抓包获取的移动端 Token 直接调用 getappmsgext API
支持短链接自动解析（Playwright fallback）
"""
import asyncio
import json
import re
import time
import requests
from typing import Dict, List, Optional
from datetime import datetime
from urllib.parse import urlparse, parse_qs


# Token 存储（运行时保存在内存中）
_wx_mobile_tokens = {
    "key": "",
    "pass_ticket": "",
    "appmsg_token": "",
    "uin": "",
    "cookie": "",
    "updated_at": None,
}

# URL参数缓存（避免重复解析同一篇文章）
_url_params_cache = {}


def set_mobile_tokens(key: str, pass_ticket: str, appmsg_token: str, uin: str, cookie: str = "") -> Dict:
    """设置微信移动端 Token（从 Fiddler 抓包获取）"""
    global _wx_mobile_tokens
    _wx_mobile_tokens = {
        "key": key.strip(),
        "pass_ticket": pass_ticket.strip(),
        "appmsg_token": appmsg_token.strip(),
        "uin": uin.strip(),
        "cookie": cookie.strip() if cookie else "",
        "updated_at": datetime.now().isoformat(),
    }
    return get_token_status()


def get_token_status() -> Dict:
    """获取当前 Token 状态"""
    has_token = bool(_wx_mobile_tokens.get("key"))
    return {
        "has_token": has_token,
        "updated_at": _wx_mobile_tokens.get("updated_at"),
        "key_preview": _wx_mobile_tokens["key"][:8] + "..." if has_token else "",
        "uin": _wx_mobile_tokens.get("uin", "")[:6] + "..." if _wx_mobile_tokens.get("uin") else "",
    }


def _extract_params_from_url(url: str) -> Optional[Dict]:
    """从URL中提取 __biz, mid, idx, sn 参数"""
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)

        _biz = params.get("__biz", [None])[0]
        mid = params.get("mid", [None])[0]
        idx = params.get("idx", [None])[0]
        sn = params.get("sn", [None])[0]

        # 正则兜底
        if not _biz:
            m = re.search(r'__biz=([^&]+)', url)
            if m: _biz = m.group(1)
        if not mid:
            m = re.search(r'mid=(\d+)', url)
            if m: mid = m.group(1)
        if not idx:
            m = re.search(r'idx=(\d+)', url)
            if m: idx = m.group(1)
        if not sn:
            m = re.search(r'sn=([^&]+)', url)
            if m: sn = m.group(1)

        if all([_biz, mid, idx, sn]):
            return {"__biz": _biz, "mid": mid, "idx": idx, "sn": sn}
        return None
    except Exception:
        return None


async def resolve_url_with_playwright(url: str) -> Optional[Dict]:
    """
    用 Playwright 加载文章页面，提取 __biz, mid, idx, sn 参数
    
    微信短链接 /s/XXXX 会跳转到验证页面，HTTP方式无法获取完整URL。
    Playwright 使用真实浏览器加载页面，可以从页面JS变量中提取参数。
    """
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.38",
                viewport={"width": 375, "height": 812}
            )
            page = await context.new_page()

            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(5000)

            params = await page.evaluate("""
                () => {
                    const result = {};
                    // 从全局变量获取
                    if (typeof window.__biz !== 'undefined') result.__biz = window.__biz;
                    if (typeof window.appmsgid !== 'undefined') result.mid = String(window.appmsgid);
                    if (typeof window.idx !== 'undefined') result.idx = String(window.idx);
                    if (typeof window.sn !== 'undefined') result.sn = window.sn;
                    
                    // 从 var 声明中提取
                    const html = document.documentElement.innerHTML;
                    if (!result.__biz) {
                        const m = html.match(/var\\s+biz\\s*=\\s*["']([^"']+)["']/);
                        if (m) result.__biz = m[1];
                    }
                    if (!result.mid) {
                        const m = html.match(/var\\s+appmsgid\\s*=\\s*["']?(\\d+)["']?/);
                        if (m) result.mid = m[1];
                    }
                    if (!result.idx) {
                        const m = html.match(/var\\s+idx\\s*=\\s*["']?(\\d+)["']?/);
                        if (m) result.idx = m[1];
                    }
                    if (!result.sn) {
                        const m = html.match(/var\\s+sn\\s*=\\s*["']([^"']+)["']/);
                        if (m) result.sn = m[1];
                    }
                    
                    // 从当前页面URL中提取
                    const currentUrl = window.location.href;
                    if (!result.__biz && currentUrl.includes('__biz=')) {
                        const u = new URL(currentUrl);
                        result.__biz = u.searchParams.get('__biz') || '';
                        result.mid = u.searchParams.get('mid') || '';
                        result.idx = u.searchParams.get('idx') || '';
                        result.sn = u.searchParams.get('sn') || '';
                    }
                    
                    result._url = currentUrl;
                    return result;
                }
            """)

            await browser.close()

            if params and params.get("__biz") and params.get("mid") and params.get("sn"):
                return {
                    "__biz": params["__biz"],
                    "mid": params["mid"],
                    "idx": params.get("idx", "1"),
                    "sn": params["sn"],
                }

            # 尝试从最终URL解析
            if params and params.get("_url"):
                return _extract_params_from_url(params["_url"])

            return None

    except Exception as e:
        print(f"Playwright URL解析失败: {e}")
        return None


async def parse_article_url(url: str) -> Optional[Dict]:
    """
    解析文章URL获取 __biz, mid, idx, sn 参数（异步，带缓存）
    
    处理流程:
    1. 检查缓存
    2. 如果URL已有参数，尝试 HTTP 重定向
    3. 如果HTTP失败，用 Playwright 解析
    """
    # 检查缓存
    if url in _url_params_cache:
        return _url_params_cache[url]

    # 如果URL已有完整参数
    if "__biz=" in url and "mid=" in url:
        result = _extract_params_from_url(url)
        if result:
            _url_params_cache[url] = result
            return result

    # HTTP 方式尝试解析
    try:
        if "__biz=" not in url:
            resp = requests.head(url, allow_redirects=True, timeout=10,
                                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
            final_url = resp.url
            if "__biz=" in final_url:
                result = _extract_params_from_url(final_url)
                if result:
                    _url_params_cache[url] = result
                    return result
    except Exception:
        pass

    # Playwright 方式解析
    result = await resolve_url_with_playwright(url)
    if result:
        _url_params_cache[url] = result
    return result


async def fetch_reading_count(url: str) -> Dict:
    """
    获取单篇文章的阅读量（异步）
    
    Returns:
        {"read_num": int, "like_num": int, "old_like_num": int, "error": str}
    """
    result = {
        "read_num": None,
        "like_num": None,
        "old_like_num": None,
        "error": ""
    }

    if not _wx_mobile_tokens.get("key"):
        result["error"] = "未设置微信移动端Token，请先通过 /api/v1/wx/reading/token 上传"
        return result

    # 解析 URL（异步）
    url_params = await parse_article_url(url)
    if not url_params:
        result["error"] = "无法从URL中解析文章参数（__biz, mid, idx, sn），Playwright解析也失败"
        return result

    try:
        api_url = "http://mp.weixin.qq.com/mp/getappmsgext"

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/81.0.4044.138 Safari/537.36 NetType/WIFI MicroMessenger/7.0.20.1781(0x6700143B) WindowsWechat(0x63070517)",
        }
        if _wx_mobile_tokens.get("cookie"):
            headers["Cookie"] = _wx_mobile_tokens["cookie"]

        params = {
            "__biz": url_params["__biz"],
            "mid": url_params["mid"],
            "sn": url_params["sn"],
            "idx": url_params["idx"],
            "key": _wx_mobile_tokens["key"],
            "pass_ticket": _wx_mobile_tokens["pass_ticket"],
            "appmsg_token": _wx_mobile_tokens["appmsg_token"],
            "uin": _wx_mobile_tokens["uin"],
            "wxtoken": "777",
        }

        data = {
            "is_only_read": "1",
            "is_temp_url": "0",
            "appmsg_type": "9",
            "reward_uin_count": "0",
        }

        # 在线程池中执行同步的 requests 调用
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: requests.post(api_url, headers=headers, data=data, params=params, timeout=15)
        )

        if resp.status_code != 200:
            result["error"] = f"API返回异常状态码: {resp.status_code}"
            return result

        content = resp.json()

        if "appmsgstat" not in content:
            base_resp = content.get("base_resp", {})
            ret = base_resp.get("ret", -1)
            if ret == -3:
                result["error"] = "Token已过期，请重新从Fiddler抓包获取"
            elif ret == -8:
                result["error"] = "请求频率过高，请稍后重试"
            else:
                err_msg = base_resp.get("err_msg", "未知错误")
                result["error"] = f"API返回错误 (ret={ret}): {err_msg}"
            return result

        stat = content["appmsgstat"]
        result["read_num"] = stat.get("read_num", 0)
        result["like_num"] = stat.get("like_num", 0)
        result["old_like_num"] = stat.get("old_like_num", 0)

    except Exception as e:
        result["error"] = f"请求异常: {str(e)}"

    return result


def save_reading_to_db(article_id: str, reading_data: Dict) -> bool:
    """将阅读量数据保存到数据库"""
    from core.db import DB
    from core.models.article import Article

    session = DB.get_session()
    try:
        article = session.query(Article).filter(Article.id == article_id).first()
        if not article:
            return False

        try:
            publish_info = json.loads(article.publish_info) if article.publish_info else {}
        except (json.JSONDecodeError, TypeError):
            publish_info = {}

        publish_info["reading_stats"] = {
            "read_num": reading_data.get("read_num"),
            "like_num": reading_data.get("like_num"),
            "old_like_num": reading_data.get("old_like_num"),
            "updated_at": datetime.now().isoformat(),
            "source": "fiddler_token"
        }

        article.publish_info = json.dumps(publish_info, ensure_ascii=False)
        article.updated_at = int(time.time())
        session.commit()
        return True

    except Exception as e:
        session.rollback()
        print(f"保存阅读量失败: {e}")
        return False
    finally:
        session.close()
