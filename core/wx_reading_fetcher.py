"""
微信阅读量直接获取模块
通过 Fiddler 抓包获取的移动端 Token 直接调用 getappmsgext API
"""
import json
import re
import time
import requests
from typing import Dict, List, Optional, Tuple
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
    "expires_at": None,  # token 过期时间
}


def set_mobile_tokens(key: str, pass_ticket: str, appmsg_token: str, uin: str, cookie: str = "") -> Dict:
    """
    设置微信移动端 Token（从 Fiddler 抓包获取）
    
    参数全部来自 Fiddler 中 getappmsgext 请求的 query params 和 headers
    """
    global _wx_mobile_tokens
    _wx_mobile_tokens = {
        "key": key.strip(),
        "pass_ticket": pass_ticket.strip(),
        "appmsg_token": appmsg_token.strip(),
        "uin": uin.strip(),
        "cookie": cookie.strip() if cookie else "",
        "updated_at": datetime.now().isoformat(),
        "expires_at": None,
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


def _resolve_short_url(url: str) -> str:
    """
    解析微信短链接，获取重定向后的完整URL
    
    微信文章短链接格式: https://mp.weixin.qq.com/s/XXXX
    完整链接格式: https://mp.weixin.qq.com/s?__biz=XXX&mid=XXX&idx=XXX&sn=XXX&...
    """
    try:
        # 如果已经有完整参数，直接返回
        if "__biz=" in url and "mid=" in url:
            return url
        
        # 跟随重定向获取完整URL（不下载body，只取Location头）
        resp = requests.head(url, allow_redirects=True, timeout=10,
                            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        final_url = resp.url
        
        # 如果head没拿到完整URL，尝试GET
        if "__biz=" not in final_url:
            resp = requests.get(url, allow_redirects=True, timeout=10, stream=True,
                               headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
            final_url = resp.url
            resp.close()
        
        return final_url
    except Exception:
        return url


def _parse_article_url(url: str) -> Optional[Dict]:
    """
    从文章 URL 中提取 getappmsgext 需要的参数
    
    支持两种格式:
    - 短链接: https://mp.weixin.qq.com/s/XXXX（自动解析重定向）
    - 完整链接: https://mp.weixin.qq.com/s?__biz=MzA3xxx==&mid=265xxx&idx=1&sn=abc123&...
    """
    try:
        # 如果是短链接，先解析重定向
        if "/s/" in url and "__biz" not in url:
            url = _resolve_short_url(url)
        
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        
        _biz = params.get("__biz", [None])[0]
        mid = params.get("mid", [None])[0]
        idx = params.get("idx", [None])[0]
        sn = params.get("sn", [None])[0]
        
        # 从完整URL中正则提取（兜底）
        if not _biz:
            m = re.search(r'__biz=([^&]+)', url)
            if m:
                _biz = m.group(1)
        
        if not mid:
            m = re.search(r'mid=(\d+)', url)
            if m:
                mid = m.group(1)
        
        if not idx:
            m = re.search(r'idx=(\d+)', url)
            if m:
                idx = m.group(1)
        
        if not sn:
            m = re.search(r'sn=([^&]+)', url)
            if m:
                sn = m.group(1)
        
        if not all([_biz, mid, idx, sn]):
            return None
        
        return {
            "__biz": _biz,
            "mid": mid,
            "idx": idx,
            "sn": sn,
        }
    except Exception:
        return None


def fetch_reading_count(url: str) -> Dict:
    """
    获取单篇文章的阅读量
    
    Returns:
        {
            "read_num": int or None,
            "like_num": int or None,
            "old_like_num": int or None,
            "error": str
        }
    """
    result = {
        "read_num": None,
        "like_num": None,
        "old_like_num": None,
        "error": ""
    }
    
    # 检查 token
    if not _wx_mobile_tokens.get("key"):
        result["error"] = "未设置微信移动端Token，请先通过 /api/v1/wx/reading/token 上传"
        return result
    
    # 解析 URL
    url_params = _parse_article_url(url)
    if not url_params:
        result["error"] = "无法从URL中解析文章参数（需要__biz, mid, idx, sn）"
        return result
    
    try:
        # 构造请求
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
        
        resp = requests.post(api_url, headers=headers, data=data, params=params, timeout=15)
        
        if resp.status_code != 200:
            result["error"] = f"API返回异常状态码: {resp.status_code}"
            return result
        
        content = resp.json()
        
        # 检查是否有错误
        if "appmsgstat" not in content:
            # Token 可能过期了
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
        
    except requests.exceptions.Timeout:
        result["error"] = "请求超时"
    except requests.exceptions.ConnectionError:
        result["error"] = "网络连接失败"
    except json.JSONDecodeError:
        result["error"] = "API返回非JSON数据"
    except Exception as e:
        result["error"] = f"未知错误: {str(e)}"
    
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
        
        # 解析已有的 publish_info
        try:
            publish_info = json.loads(article.publish_info) if article.publish_info else {}
        except (json.JSONDecodeError, TypeError):
            publish_info = {}
        
        # 更新阅读量数据
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
