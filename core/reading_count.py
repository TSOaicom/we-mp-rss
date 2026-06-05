"""
阅读量获取模块
通过 Playwright 加载文章页面，拦截 getappmsgext API 响应来获取阅读量数据
"""
import asyncio
import json
import re
import time
from typing import Dict, List, Optional
from datetime import datetime
from core.print import print_info, print_warning, print_error, print_success


async def capture_reading_count(url: str, timeout: int = 15000) -> Dict:
    """
    获取单篇文章的阅读量
    
    Args:
        url: 文章URL
        timeout: 超时时间(毫秒)
        
    Returns:
        {
            "read_num": 阅读数,
            "like_num": 点赞数,
            "old_like_num": 在看数,
            "error": 错误信息
        }
    """
    from driver.playwright_driver import PlaywrightController
    from core.config import cfg
    
    result = {
        "read_num": None,
        "like_num": None,
        "old_like_num": None,
        "error": ""
    }
    
    proxy_url = ""
    if cfg.get("proxy.enabled", False):
        proxy_url = cfg.get("proxy.http_url", "")
    
    try:
        async with PlaywrightController(
            proxy_url=proxy_url,
            mobile_mode=True
        ) as controller:
            page = await controller.open_url(url, timeout=timeout)
            if not page:
                result["error"] = "页面加载失败"
                return result
            
            p = controller.page
            
            # 拦截 getappmsgext 网络请求，捕获阅读量数据
            captured_data = {}
            
            async def handle_response(response):
                if "getappmsgext" in response.url:
                    try:
                        body = await response.text()
                        data = json.loads(body)
                        if "appmsgstat" in data:
                            captured_data.update(data["appmsgstat"])
                    except Exception:
                        pass
            
            p.on("response", handle_response)
            
            # 等待页面加载
            await asyncio.sleep(3)
            
            # 尝试从页面 JS 变量中提取阅读量
            try:
                # 方法1: 从页面的 JS 变量中提取
                js_result = await p.evaluate("""
                    () => {
                        // 尝试从页面的 appmsgstat 变量获取
                        if (window.appmsgstat) return window.appmsgstat;
                        
                        // 尝试从页面中的 script 标签提取
                        const scripts = document.querySelectorAll('script');
                        for (const s of scripts) {
                            const text = s.textContent;
                            if (text && text.includes('read_num')) {
                                const match = text.match(/"read_num"\\s*:\\s*(\\d+)/);
                                if (match) return { read_num: parseInt(match[1]) };
                            }
                        }
                        
                        // 尝试从 DOM 元素中提取
                        const readEl = document.getElementById('readNum3') 
                            || document.querySelector('.read_num')
                            || document.querySelector('#js_pc_qt');
                        if (readEl) {
                            const num = parseInt(readEl.textContent.replace(/[^\\d]/g, ''));
                            if (!isNaN(num)) return { read_num: num };
                        }
                        
                        return null;
                    }
                """)
                
                if js_result and not captured_data:
                    if "read_num" in js_result:
                        captured_data["read_num"] = js_result["read_num"]
                    if "like_num" in js_result:
                        captured_data["like_num"] = js_result["like_num"]
                    if "old_like_num" in js_result:
                        captured_data["old_like_num"] = js_result["old_like_num"]
                        
            except Exception as e:
                print_warning(f"从JS提取阅读量失败: {e}")
            
            # 方法2: 触发页面的 getappmsgext 调用
            if not captured_data:
                try:
                    await p.evaluate("""
                        () => {
                            // 尝试手动触发阅读量加载
                            if (typeof mp_profile !== 'undefined' && typeof appmsg_like !== 'undefined') {
                                appmsg_like.init();
                            }
                            // 尝试点击文章底部区域，触发阅读量显示
                            const footer = document.getElementById('js_pc_qt') 
                                || document.querySelector('.rich_media_tool');
                            if (footer) footer.click();
                        }
                    """)
                    await asyncio.sleep(3)
                except Exception:
                    pass
            
            # 方法3: 从页面 HTML 中正则提取
            if not captured_data:
                try:
                    html = await p.content()
                    # 尝试匹配 appmsgstat 数据
                    stat_match = re.search(
                        r'"appmsgstat"\s*:\s*\{[^}]*"read_num"\s*:\s*(\d+)[^}]*"like_num"\s*:\s*(\d+)',
                        html
                    )
                    if stat_match:
                        captured_data["read_num"] = int(stat_match.group(1))
                        captured_data["like_num"] = int(stat_match.group(2))
                except Exception:
                    pass
            
            # 填充结果
            if captured_data:
                result["read_num"] = captured_data.get("read_num")
                result["like_num"] = captured_data.get("like_num")
                result["old_like_num"] = captured_data.get("old_like_num")
            else:
                result["error"] = "未能获取阅读量数据（文章可能尚未被阅读或接口受限）"
                
    except Exception as e:
        result["error"] = f"获取阅读量异常: {str(e)}"
        print_error(f"获取阅读量异常: {e}")
    
    return result


async def batch_capture_reading_counts(urls: List[str], max_concurrent: int = 2, delay: float = 3.0) -> List[Dict]:
    """
    批量获取文章阅读量
    
    Args:
        urls: 文章URL列表
        max_concurrent: 最大并发数
        delay: 请求间隔(秒)
        
    Returns:
        阅读量结果列表
    """
    results = []
    
    # 使用信号量控制并发
    semaphore = asyncio.Semaphore(max_concurrent)
    
    async def fetch_one(url: str, index: int):
        async with semaphore:
            if index > 0:
                await asyncio.sleep(delay)
            print_info(f"[{index+1}/{len(urls)}] 获取阅读量: {url[:80]}...")
            result = await capture_reading_count(url)
            result["url"] = url
            return result
    
    tasks = [fetch_one(url, i) for i, url in enumerate(urls)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # 处理异常
    processed = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            processed.append({"url": urls[i], "error": str(r), "read_num": None, "like_num": None, "old_like_num": None})
        else:
            processed.append(r)
    
    return processed


def save_reading_counts_to_db(article_id: str, reading_data: Dict):
    """
    将阅读量数据保存到数据库的 publish_info 字段
    
    Args:
        article_id: 文章ID
        reading_data: 阅读量数据
    """
    from core.db import DB
    from core.models.article import Article
    
    session = DB.get_session()
    try:
        article = session.query(Article).filter(Article.id == article_id).first()
        if not article:
            print_warning(f"文章不存在: {article_id}")
            return False
        
        # 解析现有的 publish_info
        try:
            publish_info = json.loads(article.publish_info) if article.publish_info else {}
        except (json.JSONDecodeError, TypeError):
            publish_info = {}
        
        # 更新阅读量数据
        publish_info["reading_stats"] = {
            "read_num": reading_data.get("read_num"),
            "like_num": reading_data.get("like_num"),
            "old_like_num": reading_data.get("old_like_num"),
            "updated_at": datetime.now().isoformat()
        }
        
        article.publish_info = json.dumps(publish_info, ensure_ascii=False)
        article.updated_at = int(time.time())
        session.commit()
        
        print_success(f"文章 [{article.title[:30]}] 阅读量已更新: {reading_data.get('read_num', 'N/A')}")
        return True
        
    except Exception as e:
        session.rollback()
        print_error(f"保存阅读量数据失败: {e}")
        return False
    finally:
        session.close()
