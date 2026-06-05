"""
微信阅读量直接获取模块
通过 Fiddler 抓包获取的移动端 Token 直接调用 getappmsgext API

URL 参数获取策略（按优先级）：
1. URL映射表（用户手动上传完整URL）
2. URL中直接提取（如果已经是完整URL）
3. HTTP重定向跟踪（多种UA策略）
4. 验证码页面HTML解析（提取嵌入的重定向URL）
5. extinfo中存储的元数据（从 appmsgpublish API 采集时保存）
"""
import asyncio
import json
import re
import time
import requests
from typing import Dict, List, Optional, Tuple
from datetime import datetime
from urllib.parse import urlparse, parse_qs, unquote


# ========== Token 管理 ==========

_wx_mobile_tokens = {
    "key": "",
    "pass_ticket": "",
    "appmsg_token": "",
    "uin": "",
    "cookie": "",
    "updated_at": None,
}

# URL 参数缓存
_url_params_cache = {}

# URL 映射表：短链接 -> 完整URL参数
# 持久化到文件以避免重启丢失
_url_mapping_file = "/app/data/url_mapping.json"
_url_mapping = {}


def _load_url_mapping():
    """从文件加载URL映射"""
    global _url_mapping
    try:
        import os
        if os.path.exists(_url_mapping_file):
            with open(_url_mapping_file, 'r', encoding='utf-8') as f:
                _url_mapping = json.load(f)
    except Exception as e:
        print(f"加载URL映射失败: {e}")
        _url_mapping = {}


def _save_url_mapping():
    """保存URL映射到文件"""
    try:
        import os
        os.makedirs(os.path.dirname(_url_mapping_file), exist_ok=True)
        with open(_url_mapping_file, 'w', encoding='utf-8') as f:
            json.dump(_url_mapping, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存URL映射失败: {e}")


# 启动时加载映射
_load_url_mapping()


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
        "url_mapping_count": len(_url_mapping),
    }


# ========== URL 映射管理 ==========

def add_url_mapping(short_url: str, full_url: str) -> bool:
    """
    添加 URL 映射（短链接 -> 完整URL）
    
    Args:
        short_url: 短链接，如 https://mp.weixin.qq.com/s/XXXX
        full_url: 完整URL，包含 __biz, mid, idx, sn 参数
    """
    params = _extract_params_from_url(full_url)
    if not params:
        return False
    
    # 提取短链接的关键部分
    key = _normalize_short_url(short_url)
    _url_mapping[key] = {
        "full_url": full_url,
        "params": params,
        "added_at": datetime.now().isoformat(),
    }
    _save_url_mapping()
    return True


def add_url_mapping_batch(mappings: List[Dict]) -> Tuple[int, int]:
    """
    批量添加 URL 映射
    
    Args:
        mappings: [{"short_url": "...", "full_url": "..."}, ...]
    
    Returns:
        (成功数, 失败数)
    """
    success = 0
    failed = 0
    for m in mappings:
        if add_url_mapping(m.get("short_url", ""), m.get("full_url", "")):
            success += 1
        else:
            failed += 1
    return success, failed


def get_url_mapping_stats() -> Dict:
    """获取 URL 映射统计"""
    return {
        "total": len(_url_mapping),
        "sample": list(_url_mapping.keys())[:5],
    }


def _normalize_short_url(url: str) -> str:
    """标准化短链接，提取 /s/ 后面的部分"""
    m = re.search(r'/s/([A-Za-z0-9_-]+)', url)
    if m:
        return f"/s/{m.group(1)}"
    # 如果已经是完整URL，用完整URL作为key
    return url


def _lookup_url_mapping(url: str) -> Optional[Dict]:
    """查找URL映射"""
    key = _normalize_short_url(url)
    mapping = _url_mapping.get(key)
    if mapping:
        return mapping.get("params")
    
    # 尝试完整URL匹配
    if url in _url_mapping:
        return _url_mapping[url].get("params")
    
    return None


# ========== URL 参数提取 ==========

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


def _extract_from_captcha_html(html: str) -> Optional[str]:
    """
    从微信验证码页面 HTML 中提取目标文章URL
    
    微信验证码页面通常包含：
    1. meta refresh 标签指向目标URL
    2. JavaScript 中的重定向URL
    3. 隐藏表单字段
    """
    if not html:
        return None
    
    # 从 meta refresh 提取
    m = re.search(r'<meta[^>]*url=([^"\'>\s]+)', html, re.I)
    if m:
        target = unquote(m.group(1))
        if '__biz' in target:
            return target
    
    # 从 redirect_url / return_url 变量提取
    patterns = [
        r'redirect_url\s*[=:]\s*["\']([^"\']+)["\']',
        r'return_url\s*[=:]\s*["\']([^"\']+)["\']',
        r'next_url\s*[=:]\s*["\']([^"\']+)["\']',
        r'target_url\s*[=:]\s*["\']([^"\']+)["\']',
        r'window\.location\.href\s*=\s*["\']([^"\']+)["\']',
        r'window\.location\.replace\s*\(\s*["\']([^"\']+)["\']',
        r'window\.location\s*=\s*["\']([^"\']+)["\']',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.I)
        if m:
            target = unquote(m.group(1))
            if '__biz' in target:
                return target
    
    # 从 data-url 属性提取
    m = re.search(r'data-url=["\']([^"\']+)["\']', html)
    if m:
        target = unquote(m.group(1))
        if '__biz' in target:
            return target
    
    return None


def _extract_biz_from_html(html: str) -> Optional[Dict]:
    """
    从页面HTML中直接提取文章参数（即使页面被验证码拦截）
    
    微信文章页面的JS变量通常包含这些值
    """
    if not html:
        return None
    
    result = {}
    
    # 提取 __biz
    m = re.search(r'var\s+biz\s*=\s*["\']([^"\']+)["\']', html)
    if m:
        result["__biz"] = m.group(1)
    else:
        m = re.search(r'__biz=([^&"\'\\s]+)', html)
        if m:
            result["__biz"] = m.group(1)
    
    # 提取 appmsgid / mid
    m = re.search(r'var\s+appmsgid\s*=\s*["\']?(\d+)["\']?', html)
    if m:
        result["mid"] = m.group(1)
    else:
        m = re.search(r'mid=(\d+)', html)
        if m:
            result["mid"] = m.group(1)
    
    # 提取 idx
    m = re.search(r'var\s+idx\s*=\s*["\']?(\d+)["\']?', html)
    if m:
        result["idx"] = m.group(1)
    else:
        m = re.search(r'idx=(\d+)', html)
        if m:
            result["idx"] = m.group(1)
    
    # 提取 sn
    m = re.search(r'var\s+sn\s*=\s*["\']([^"\']+)["\']', html)
    if m:
        result["sn"] = m.group(1)
    else:
        m = re.search(r'sn=([^&"\'\\s]+)', html)
        if m:
            result["sn"] = m.group(1)
    
    if all(k in result for k in ["__biz", "mid", "sn"]):
        if "idx" not in result:
            result["idx"] = "1"
        return result
    
    return None


# ========== HTTP URL 解析（多策略） ==========

_MOBILE_UAS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.43(0x1800002b) NetType/WIFI Language/zh_CN",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.43 Mobile Safari/537.36 MicroMessenger/8.0.43",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.38",
]

_DESKTOP_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
]


def _resolve_url_http(url: str) -> Tuple[Optional[Dict], Dict]:
    """
    通过 HTTP 请求解析短链接（多策略）
    
    Returns:
        (解析结果, 诊断信息)
    """
    diag = {
        "strategies_tried": [],
        "final_urls": [],
        "captcha_detected": False,
        "html_extracted": False,
    }
    
    # 如果URL已经有参数，直接提取
    if "__biz=" in url and "mid=" in url:
        result = _extract_params_from_url(url)
        if result:
            return result, {"strategy": "direct"}
    
    # 策略1: 多种UA + GET请求跟踪重定向
    for i, ua in enumerate(_MOBILE_UAS[:2]):
        strategy_name = f"mobile_ua_{i}"
        diag["strategies_tried"].append(strategy_name)
        try:
            resp = requests.get(
                url,
                allow_redirects=True,
                timeout=15,
                headers={
                    "User-Agent": ua,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                }
            )
            final_url = resp.url
            diag["final_urls"].append({"strategy": strategy_name, "url": final_url[:200]})
            
            # 检查是否到达真实文章页
            if "__biz=" in final_url:
                result = _extract_params_from_url(final_url)
                if result:
                    return result, {"strategy": strategy_name}
            
            # 检查验证码页面
            if "appmsgcaptcha" in final_url or "环境异常" in (resp.text or ""):
                diag["captcha_detected"] = True
                
                # 尝试从验证码页面HTML提取参数
                captcha_params = _extract_from_captcha_html(resp.text)
                if captcha_params:
                    result = _extract_params_from_url(captcha_params)
                    if result:
                        return result, {"strategy": f"{strategy_name}_captcha_redirect"}
                
                # 尝试直接从HTML提取参数
                html_params = _extract_biz_from_html(resp.text)
                if html_params:
                    diag["html_extracted"] = True
                    return html_params, {"strategy": f"{strategy_name}_html_extract"}
            
            # 如果响应200且没有验证码，尝试从HTML提取
            if resp.status_code == 200 and "appmsgcaptcha" not in final_url:
                html_params = _extract_biz_from_html(resp.text)
                if html_params:
                    return html_params, {"strategy": f"{strategy_name}_page_html"}
                    
        except Exception as e:
            diag["strategies_tried"].append(f"{strategy_name}_error: {str(e)[:100]}")
    
    # 策略2: 桌面UA
    for i, ua in enumerate(_DESKTOP_UAS[:1]):
        strategy_name = f"desktop_ua_{i}"
        diag["strategies_tried"].append(strategy_name)
        try:
            resp = requests.get(
                url,
                allow_redirects=True,
                timeout=15,
                headers={
                    "User-Agent": ua,
                    "Accept": "text/html",
                }
            )
            final_url = resp.url
            diag["final_urls"].append({"strategy": strategy_name, "url": final_url[:200]})
            
            if "__biz=" in final_url:
                result = _extract_params_from_url(final_url)
                if result:
                    return result, {"strategy": strategy_name}
            
            if resp.status_code == 200:
                html_params = _extract_biz_from_html(resp.text)
                if html_params:
                    return html_params, {"strategy": f"{strategy_name}_html"}
                    
        except Exception as e:
            diag["strategies_tried"].append(f"{strategy_name}_error: {str(e)[:100]}")
    
    # 策略3: HEAD请求（不下载body，只看Location头）
    diag["strategies_tried"].append("head_request")
    try:
        resp = requests.head(url, allow_redirects=True, timeout=10,
                            headers={"User-Agent": _MOBILE_UAS[0]})
        final_url = resp.url
        diag["final_urls"].append({"strategy": "head", "url": final_url[:200]})
        if "__biz=" in final_url:
            result = _extract_params_from_url(final_url)
            if result:
                return result, {"strategy": "head_redirect"}
    except Exception as e:
        diag["strategies_tried"].append(f"head_error: {str(e)[:100]}")
    
    return None, diag


# ========== 主入口：解析文章URL ==========

async def parse_article_url(url: str) -> Tuple[Optional[Dict], Dict]:
    """
    解析文章URL获取 __biz, mid, idx, sn 参数
    
    处理流程（按优先级）：
    1. 检查URL映射表
    2. 检查缓存
    3. 如果URL已有参数，直接提取
    4. HTTP多策略解析
    5. 从extinfo中读取（由调用者处理）
    
    Returns:
        (解析结果, 诊断信息)
    """
    diag = {"url": url[:200], "steps": []}
    
    # 1. 检查URL映射表
    mapped = _lookup_url_mapping(url)
    if mapped:
        diag["steps"].append("url_mapping_hit")
        _url_params_cache[url] = mapped
        return mapped, diag
    
    # 2. 检查缓存
    if url in _url_params_cache:
        diag["steps"].append("cache_hit")
        return _url_params_cache[url], diag
    
    # 3. 如果URL已有完整参数
    if "__biz=" in url and "mid=" in url:
        result = _extract_params_from_url(url)
        if result:
            diag["steps"].append("direct_extract")
            _url_params_cache[url] = result
            return result, diag
    
    # 4. HTTP多策略解析
    diag["steps"].append("http_resolve")
    result, http_diag = _resolve_url_http(url)
    diag["http_diag"] = http_diag
    
    if result:
        _url_params_cache[url] = result
        return result, diag
    
    # 5. 尝试从extinfo获取
    diag["steps"].append("no_result")
    return None, diag


def parse_article_url_sync(url: str) -> Optional[Dict]:
    """同步版本，用于批量任务"""
    # 1. URL映射
    mapped = _lookup_url_mapping(url)
    if mapped:
        return mapped
    
    # 2. 缓存
    if url in _url_params_cache:
        return _url_params_cache[url]
    
    # 3. 直接提取
    if "__biz=" in url and "mid=" in url:
        result = _extract_params_from_url(url)
        if result:
            _url_params_cache[url] = result
            return result
    
    # 4. HTTP解析
    result, _ = _resolve_url_http(url)
    if result:
        _url_params_cache[url] = result
        return result
    
    return None


# ========== 阅读量获取 ==========

async def fetch_reading_count(url: str) -> Dict:
    """
    获取单篇文章的阅读量（异步）
    
    Returns:
        {"read_num": int, "like_num": int, "old_like_num": int, "error": str, "diag": dict}
    """
    result = {
        "read_num": None,
        "like_num": None,
        "old_like_num": None,
        "error": "",
        "diag": {},
    }

    if not _wx_mobile_tokens.get("key"):
        result["error"] = "未设置微信移动端Token，请先通过 /api/v1/wx/reading/token 上传"
        return result

    # 解析 URL
    url_params, parse_diag = await parse_article_url(url)
    result["diag"] = parse_diag
    
    if not url_params:
        result["error"] = (
            "无法从URL中解析文章参数（__biz, mid, idx, sn）。"
            "请通过 /api/v1/wx/reading/url-mapping 上传完整文章URL映射。"
        )
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


# ========== extinfo 管理 ==========

def get_article_extinfo(article_id: str) -> Optional[Dict]:
    """从数据库读取文章的 extinfo 字段"""
    from core.db import DB
    from core.models.article import Article
    
    session = DB.get_session()
    try:
        article = session.query(Article).filter(Article.id == article_id).first()
        if not article or not article.extinfo:
            return None
        return json.loads(article.extinfo)
    except (json.JSONDecodeError, TypeError):
        return None
    finally:
        session.close()


def get_url_params_from_extinfo(article_id: str) -> Optional[Dict]:
    """从 extinfo 中获取 URL 参数"""
    ext = get_article_extinfo(article_id)
    if not ext:
        return None
    params = ext.get("url_params")
    if params and all(k in params for k in ["__biz", "mid", "sn"]):
        if "idx" not in params:
            params["idx"] = "1"
        return params
    return None


def save_url_params_to_extinfo(article_id: str, params: Dict) -> bool:
    """将 URL 参数保存到 extinfo"""
    from core.db import DB
    from core.models.article import Article
    
    session = DB.get_session()
    try:
        article = session.query(Article).filter(Article.id == article_id).first()
        if not article:
            return False
        
        try:
            extinfo = json.loads(article.extinfo) if article.extinfo else {}
        except (json.JSONDecodeError, TypeError):
            extinfo = {}
        
        extinfo["url_params"] = params
        article.extinfo = json.dumps(extinfo, ensure_ascii=False)
        article.updated_at = int(time.time())
        session.commit()
        return True
    except Exception as e:
        session.rollback()
        print(f"保存extinfo失败: {e}")
        return False
    finally:
        session.close()


# ========== 诊断工具 ==========

def diagnose_url_resolution(url: str) -> Dict:
    """同步诊断URL解析过程"""
    result = {
        "url": url[:200],
        "steps": [],
    }
    
    # 1. URL映射
    mapped = _lookup_url_mapping(url)
    if mapped:
        result["steps"].append({"step": "url_mapping", "status": "found", "params": mapped})
        return result
    result["steps"].append({"step": "url_mapping", "status": "not_found"})
    
    # 2. 直接提取
    if "__biz=" in url and "mid=" in url:
        params = _extract_params_from_url(url)
        if params:
            result["steps"].append({"step": "direct_extract", "status": "found", "params": params})
            return result
    
    # 3. HTTP解析
    params, http_diag = _resolve_url_http(url)
    result["steps"].append({
        "step": "http_resolve",
        "status": "found" if params else "not_found",
        "params": params,
        "diag": http_diag,
    })
    
    return result
