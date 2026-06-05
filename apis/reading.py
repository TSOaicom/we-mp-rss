"""
阅读量 API 模块
提供 Token 管理、URL映射管理、文章阅读量获取和诊断接口
"""
import threading
import json
import time
from fastapi import APIRouter, Depends, HTTPException, status, Query, Body
from core.auth import get_current_user_or_ak
from core.db import DB
from .base import success_response, error_response

router = APIRouter(prefix="/reading", tags=["阅读量管理"])

# 后台任务管理
_reading_tasks = {}
_reading_tasks_lock = threading.Lock()


def _set_reading_task(task_id: str, data: dict):
    with _reading_tasks_lock:
        _reading_tasks[task_id] = data


def _get_reading_task(task_id: str) -> dict:
    with _reading_tasks_lock:
        return _reading_tasks.get(task_id, {})


# ========== Token 管理 ==========

@router.post("/token", summary="上传微信移动端 Token")
async def upload_mobile_token(
    key: str = Body(..., description="从 Fiddler getappmsgext 请求的 query 参数中获取"),
    pass_ticket: str = Body(..., description="从 Fiddler getappmsgext 请求的 query 参数中获取"),
    appmsg_token: str = Body(..., description="从 Fiddler getappmsgext 请求的 query 参数中获取"),
    uin: str = Body(..., description="从 Fiddler getappmsgext 请求的 query 参数中获取"),
    cookie: str = Body("", description="从 Fiddler getappmsgext 请求的 Cookie header 中获取（可选）"),
    current_user: dict = Depends(get_current_user_or_ak)
):
    """
    上传微信移动端 Token（从 Fiddler 抓包获取）
    
    操作步骤：
    1. 电脑安装 Fiddler，手机和电脑连同一 WiFi
    2. 手机 WiFi 设置 HTTP 代理指向 Fiddler
    3. 手机微信打开任意一篇公众号文章
    4. 在 Fiddler 中找到 mp.weixin.qq.com/mp/getappmsgext 请求
    5. 从 URL query 参数中复制 key、pass_ticket、appmsg_token、uin
    6. 从请求头中复制 Cookie（可选，提高成功率）
    """
    from core.wx_reading_fetcher import set_mobile_tokens
    
    result = set_mobile_tokens(
        key=key,
        pass_ticket=pass_ticket,
        appmsg_token=appmsg_token,
        uin=uin,
        cookie=cookie
    )
    
    return success_response({
        **result,
        "message": "Token 已保存，可以开始批量获取阅读量。Token 有效期通常为数小时。"
    })


@router.get("/token/status", summary="查看 Token 状态")
async def get_token_status(
    current_user: dict = Depends(get_current_user_or_ak)
):
    """查看当前微信移动端 Token 的状态"""
    from core.wx_reading_fetcher import get_token_status
    return success_response(get_token_status())


@router.post("/token/test", summary="测试 Token 是否有效")
async def test_token(
    test_url: str = Body(..., description="任意一篇微信文章的完整URL（含 __biz, mid, idx, sn 参数），用于测试Token有效性"),
    current_user: dict = Depends(get_current_user_or_ak)
):
    """
    用一篇已知完整URL的文章测试Token是否有效。
    
    注意：test_url 必须是包含 __biz, mid, idx, sn 参数的完整URL，
    不能是短链接（如 /s/XXXX），因为短链接需要验证码解析。
    """
    from core.wx_reading_fetcher import fetch_reading_count
    
    result = await fetch_reading_count(test_url)
    
    if result.get("read_num") is not None:
        return success_response({
            "valid": True,
            "read_num": result["read_num"],
            "like_num": result.get("like_num"),
            "old_like_num": result.get("old_like_num"),
            "message": "Token 有效！可以开始批量获取阅读量。"
        })
    else:
        return success_response({
            "valid": False,
            "error": result.get("error", "获取失败"),
            "diag": result.get("diag", {}),
            "message": "Token 无效或URL参数不完整，请检查。"
        })


# ========== URL 映射管理 ==========

@router.post("/url-mapping", summary="上传文章URL映射")
async def upload_url_mapping(
    mappings: list = Body(..., description='URL映射列表，格式: [{"short_url": "https://mp.weixin.qq.com/s/XXXX", "full_url": "https://mp.weixin.qq.com/s?__biz=...&mid=...&idx=...&sn=..."}]'),
    current_user: dict = Depends(get_current_user_or_ak)
):
    """
    上传文章 URL 映射（短链接 -> 完整URL）
    
    用于解决微信短链接验证码拦截问题。
    
    获取完整URL的方法：
    1. 在手机微信中打开文章 → 点右上角"..." → "复制链接" → 得到完整URL
    2. 在 Fiddler 中找到 getappmsgext 请求，URL中包含完整参数
    3. 在电脑浏览器中打开文章 → 地址栏复制完整URL
    
    每篇需要获取阅读量的文章都需要上传一条映射。
    """
    from core.wx_reading_fetcher import add_url_mapping_batch
    
    if not isinstance(mappings, list) or len(mappings) == 0:
        return error_response(40001, "mappings 必须是非空列表")
    
    # 限制单次批量大小
    if len(mappings) > 500:
        return error_response(40001, "单次最多上传500条映射")
    
    success, failed = add_url_mapping_batch(mappings)
    
    return success_response({
        "total": len(mappings),
        "success": success,
        "failed": failed,
        "message": f"已添加 {success} 条URL映射" + (f"，{failed} 条失败（完整URL格式不正确）" if failed > 0 else "")
    })


@router.post("/url-mapping/auto", summary="自动从 Fiddler 日志批量提取URL映射")
async def auto_url_mapping_from_fiddler(
    fiddler_urls: list = Body(..., description='Fiddler 中 getappmsgext 请求的完整URL列表，每个URL都包含 __biz, mid, idx, sn 参数'),
    current_user: dict = Depends(get_current_user_or_ak)
):
    """
    从 Fiddler 抓包日志中批量提取URL映射。
    
    使用方法：
    1. 在手机上快速浏览多篇公众号文章（通过微信代理到Fiddler）
    2. 在 Fiddler 中过滤 mp.weixin.qq.com/mp/getappmsgext 请求
    3. 导出所有匹配的URL列表
    4. 将URL列表上传到此接口
    
    系统会自动从URL中提取 __biz, mid, idx, sn 并建立映射。
    """
    from core.wx_reading_fetcher import add_url_mapping, _extract_params_from_url
    
    success = 0
    failed = 0
    
    for full_url in fiddler_urls:
        if not isinstance(full_url, str):
            failed += 1
            continue
        
        params = _extract_params_from_url(full_url)
        if params:
            # 用完整URL本身作为映射（不需要短链接对应）
            add_url_mapping(full_url, full_url)
            success += 1
        else:
            failed += 1
    
    return success_response({
        "total": len(fiddler_urls),
        "success": success,
        "failed": failed,
    })


@router.get("/url-mapping/stats", summary="URL映射统计")
async def get_url_mapping_stats(
    current_user: dict = Depends(get_current_user_or_ak)
):
    """查看URL映射表统计信息"""
    from core.wx_reading_fetcher import get_url_mapping_stats
    return success_response(get_url_mapping_stats())


# ========== 诊断 ==========

@router.post("/diagnose", summary="诊断URL解析")
async def diagnose_url(
    url: str = Body(..., description="要诊断的文章URL"),
    current_user: dict = Depends(get_current_user_or_ak)
):
    """
    诊断单个URL的解析过程，显示每一步的尝试结果。
    
    用于排查为什么某篇文章无法获取阅读量。
    """
    from core.wx_reading_fetcher import diagnose_url_resolution
    
    result = diagnose_url_resolution(url)
    return success_response(result)


@router.get("/diagnose/db", summary="诊断数据库文章数据")
async def diagnose_db(
    mp_id: str = Query(None, description="公众号ID（可选，不传则返回全局样本）"),
    limit: int = Query(5, ge=1, le=20),
    current_user: dict = Depends(get_current_user_or_ak)
):
    """
    查看数据库中文章的原始数据，帮助诊断 URL 参数问题。
    
    返回文章的 url、publish_info、extinfo 等字段的原始内容。
    """
    session = DB.get_session()
    try:
        from core.models.article import Article
        from core.models.feed import Feed
        
        query = session.query(Article)
        if mp_id:
            query = query.filter(Article.mp_id == mp_id)
            feed = session.query(Feed).filter(Feed.id == mp_id).first()
            mp_name = feed.mp_name if feed else "未知"
        else:
            mp_name = "全部"
        
        articles = query.filter(
            Article.url != None,
            Article.url != ""
        ).order_by(Article.publish_time.desc()).limit(limit).all()
        
        samples = []
        for a in articles:
            # 解析 publish_info 查看内容
            pub_info = None
            try:
                pub_info = json.loads(a.publish_info) if a.publish_info else None
            except (json.JSONDecodeError, TypeError):
                pub_info = a.publish_info
            
            # 解析 extinfo
            ext_info = None
            try:
                ext_info = json.loads(a.extinfo) if a.extinfo else None
            except (json.JSONDecodeError, TypeError):
                ext_info = a.extinfo
            
            # 检查 publish_info 中是否有 URL 参数相关字段
            has_url_params_in_pub = False
            if isinstance(pub_info, dict):
                for key in ["__biz", "mid", "sn", "content_url", "msg_link"]:
                    if key in pub_info:
                        has_url_params_in_pub = True
                        break
            
            samples.append({
                "id": a.id,
                "mp_id": a.mp_id,
                "title": a.title[:80] if a.title else "",
                "url": a.url,
                "url_type": "full" if "__biz=" in (a.url or "") else "short",
                "publish_info_type": type(pub_info).__name__,
                "publish_info_keys": list(pub_info.keys()) if isinstance(pub_info, dict) else None,
                "has_url_params_in_publish_info": has_url_params_in_pub,
                "extinfo": ext_info,
                "publish_time": a.publish_time,
            })
        
        return success_response({
            "mp_id": mp_id,
            "mp_name": mp_name,
            "count": len(samples),
            "articles": samples,
        })
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_201_CREATED,
            detail=error_response(50001, f"诊断失败: {str(e)}")
        )
    finally:
        session.close()


@router.get("/diagnose/api-response", summary="诊断 appmsgpublish API 响应")
async def diagnose_api_response(
    mp_id: str = Query(..., description="公众号ID"),
    current_user: dict = Depends(get_current_user_or_ak)
):
    """
    调用 appmsgpublish API 获取原始响应，查看所有可用字段。
    
    帮助确认 API 是否返回 content_url 等包含完整URL参数的字段。
    """
    session = DB.get_session()
    try:
        from core.models.feed import Feed
        
        feed = session.query(Feed).filter(Feed.id == mp_id).first()
        if not feed:
            raise HTTPException(status_code=404, detail=error_response(40401, "公众号不存在"))
        
        faker_id = feed.faker_id
        if not faker_id:
            return error_response(40001, "该公众号没有 faker_id，无法调用 API")
        
        # 尝试调用 appmsgpublish API
        try:
            from core.wx.base import WxGather
            from driver.token import get as get_token_val
            
            token = get_token_val('token', '')
            cookies = get_token_val('cookie', '')
            
            if not token:
                return error_response(40001, "微信MP平台Token未设置，请先扫码登录")
            
            import requests as req
            url = "https://mp.weixin.qq.com/cgi-bin/appmsgpublish"
            params = {
                "sub": "list",
                "sub_action": "list_ex",
                "begin": 0,
                "count": 1,  # 只取1篇用于诊断
                "fakeid": faker_id,
                "token": token,
                "lang": "zh_CN",
                "f": "json",
                "ajax": 1
            }
            headers = {
                "Cookie": cookies,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": url,
            }
            
            resp = req.get(url, headers=headers, params=params, verify=False, timeout=30)
            msg = resp.json()
            
            if msg.get('base_resp', {}).get('ret', -1) != 0:
                return error_response(
                    50002,
                    f"API返回错误: ret={msg['base_resp'].get('ret')}, msg={msg['base_resp'].get('err_msg', '')}"
                )
            
            # 解析并返回原始数据
            result = {
                "mp_id": mp_id,
                "mp_name": feed.mp_name,
                "faker_id": faker_id,
                "api_ret": msg.get('base_resp', {}).get('ret'),
            }
            
            if 'publish_page' in msg:
                import json as j
                publish_page = j.loads(msg['publish_page'])
                publish_list = publish_page.get('publish_list', [])
                
                if publish_list:
                    first_item = publish_list[0]
                    publish_info = j.loads(first_item.get('publish_info', '{}'))
                    
                    # 返回 publish_info 的所有顶级 key
                    result["publish_info_keys"] = list(publish_info.keys())
                    
                    # 返回 appmsgex 第一篇的所有字段
                    appmsgex = publish_info.get('appmsgex', [])
                    if appmsgex:
                        first_article = appmsgex[0]
                        result["first_article_keys"] = list(first_article.keys())
                        result["first_article"] = {
                            k: str(v)[:200] if isinstance(v, (str, dict, list)) else v
                            for k, v in first_article.items()
                        }
                        
                        # 特别检查是否有 content_url 或类似字段
                        url_fields = {}
                        for k, v in first_article.items():
                            if isinstance(v, str) and ('url' in k.lower() or 'link' in k.lower() or 'http' in v[:10]):
                                url_fields[k] = v[:200]
                        result["url_like_fields"] = url_fields
                    
                    # 返回内层 publish_info 字段
                    inner_pub = publish_info.get('publish_info', '')
                    if inner_pub:
                        result["inner_publish_info_sample"] = str(inner_pub)[:500]
                
                result["total_pages"] = len(publish_list)
            else:
                result["message"] = "API返回中没有 publish_page 字段"
            
            return success_response(result)
            
        except ImportError as e:
            return error_response(50001, f"模块导入失败: {str(e)}")
        except Exception as e:
            return error_response(50001, f"API调用失败: {str(e)}")
            
    finally:
        session.close()


# ========== 批量阅读量获取 ==========

@router.post("/batch", summary="批量获取公众号文章阅读量")
async def batch_get_reading(
    mp_id: str = Body(..., description="公众号ID"),
    max_articles: int = Body(100, description="最多获取文章数", ge=1, le=500),
    current_user: dict = Depends(get_current_user_or_ak)
):
    """
    批量获取指定公众号文章的阅读量（后台异步执行）
    
    需要先：
    1. 通过 /token 接口上传微信移动端 Token
    2. 通过 /url-mapping 接口上传文章URL映射（或使用完整URL的文章）
    """
    import uuid
    from core.wx_reading_fetcher import get_token_status
    
    # 检查 Token
    token_status = get_token_status()
    if not token_status["has_token"]:
        return error_response(40002, "未设置微信移动端Token，请先通过 POST /api/v1/wx/reading/token 上传")
    
    session = DB.get_session()
    try:
        from core.models.article import Article
        from core.models.feed import Feed
        
        feed = session.query(Feed).filter(Feed.id == mp_id).first()
        if not feed:
            raise HTTPException(status_code=404, detail=error_response(40401, "公众号不存在"))
        
        # 获取文章（优先获取没有阅读量数据的）
        articles = session.query(Article).filter(
            Article.mp_id == mp_id,
            Article.url != None,
            Article.url != ""
        ).order_by(Article.publish_time.desc()).limit(max_articles).all()
        
        if not articles:
            return error_response(40001, "该公众号没有可获取阅读量的文章")
        
        # 创建后台任务
        task_id = str(uuid.uuid4())
        _set_reading_task(task_id, {
            "task_id": task_id,
            "mp_id": mp_id,
            "mp_name": feed.mp_name,
            "total": len(articles),
            "processed": 0,
            "success": 0,
            "failed": 0,
            "skipped_no_url_params": 0,
            "status": "running",
            "results": [],
            "created_at": __import__('datetime').datetime.now().isoformat()
        })
        
        # 准备文章数据
        article_data = [
            {"id": a.id, "url": a.url, "title": a.title[:60]}
            for a in articles
        ]
        
        # 启动后台线程
        def run_batch_task():
            _run_batch_reading_task_sync(task_id, article_data)
        
        thread = threading.Thread(target=run_batch_task, daemon=True)
        thread.start()
        
        # 统计URL类型
        url_types = {"full": 0, "short": 0, "mapped": 0}
        from core.wx_reading_fetcher import _lookup_url_mapping
        for a in article_data:
            if "__biz=" in (a["url"] or ""):
                url_types["full"] += 1
            elif _lookup_url_mapping(a["url"]):
                url_types["mapped"] += 1
            else:
                url_types["short"] += 1
        
        return success_response({
            "task_id": task_id,
            "mp_id": mp_id,
            "mp_name": feed.mp_name,
            "total_articles": len(articles),
            "url_types": url_types,
            "status": "running",
            "message": (
                f"已开始获取 {len(articles)} 篇文章的阅读量。"
                f"URL情况：完整URL {url_types['full']} 篇，已映射 {url_types['mapped']} 篇，"
                f"短链接未映射 {url_types['short']} 篇（这些会解析失败）。"
            )
        })
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_201_CREATED,
            detail=error_response(50001, f"启动批量获取失败: {str(e)}")
        )


@router.post("/batch/all", summary="批量获取所有公众号文章阅读量")
async def batch_get_all_reading(
    max_articles_per_mp: int = Body(50, description="每个公众号最多获取文章数", ge=1, le=200),
    current_user: dict = Depends(get_current_user_or_ak)
):
    """
    批量获取所有公众号文章的阅读量（后台异步执行）
    """
    import uuid
    from core.wx_reading_fetcher import get_token_status
    
    token_status = get_token_status()
    if not token_status["has_token"]:
        return error_response(40002, "未设置微信移动端Token，请先通过 POST /api/v1/wx/reading/token 上传")
    
    session = DB.get_session()
    try:
        from core.models.article import Article
        from core.models.feed import Feed
        
        feeds = session.query(Feed).filter(Feed.status == 1).all()
        
        all_articles = []
        for feed in feeds:
            articles = session.query(Article).filter(
                Article.mp_id == feed.id,
                Article.url != None,
                Article.url != ""
            ).order_by(Article.publish_time.desc()).limit(max_articles_per_mp).all()
            
            for a in articles:
                all_articles.append({
                    "id": a.id,
                    "url": a.url,
                    "title": a.title[:60],
                    "mp_id": feed.id,
                    "mp_name": feed.mp_name,
                })
        
        if not all_articles:
            return error_response(40001, "没有可获取阅读量的文章")
        
        task_id = str(uuid.uuid4())
        _set_reading_task(task_id, {
            "task_id": task_id,
            "mp_id": "all",
            "mp_name": "全部公众号",
            "total": len(all_articles),
            "processed": 0,
            "success": 0,
            "failed": 0,
            "status": "running",
            "results": [],
            "created_at": __import__('datetime').datetime.now().isoformat()
        })
        
        def run_all_task():
            _run_batch_reading_task_sync(task_id, all_articles)
        
        thread = threading.Thread(target=run_all_task, daemon=True)
        thread.start()
        
        return success_response({
            "task_id": task_id,
            "total_mps": len(feeds),
            "total_articles": len(all_articles),
            "status": "running",
            "message": f"已开始获取 {len(feeds)} 个公众号共 {len(all_articles)} 篇文章的阅读量"
        })
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_201_CREATED,
            detail=error_response(50001, f"启动批量获取失败: {str(e)}")
        )


def _run_batch_reading_task_sync(task_id: str, article_data: list):
    """异步方式执行批量阅读量获取（在线程中运行 async event loop）"""
    import asyncio
    from core.wx_reading_fetcher import fetch_reading_count, save_reading_to_db, parse_article_url_sync
    
    async def _run():
        task = _get_reading_task(task_id)
        results = []
        consecutive_errors = 0
        
        for i, article in enumerate(article_data):
            try:
                reading_result = await fetch_reading_count(article["url"])
                
                if reading_result.get("read_num") is not None:
                    save_reading_to_db(article["id"], reading_result)
                    
                    results.append({
                        "article_id": article["id"],
                        "title": article.get("title", ""),
                        "read_num": reading_result["read_num"],
                        "like_num": reading_result.get("like_num"),
                        "old_like_num": reading_result.get("old_like_num"),
                        "status": "success"
                    })
                    task["success"] += 1
                    consecutive_errors = 0
                else:
                    err = reading_result.get("error", "未知")
                    
                    # 区分"URL参数缺失"和其他错误
                    if "无法从URL中解析" in err:
                        task["skipped_no_url_params"] = task.get("skipped_no_url_params", 0) + 1
                        results.append({
                            "article_id": article["id"],
                            "title": article.get("title", ""),
                            "read_num": None,
                            "error": "短链接无法解析，需要上传URL映射",
                            "status": "skipped"
                        })
                        # 短链接未映射不算连续错误
                        continue
                    
                    results.append({
                        "article_id": article["id"],
                        "title": article.get("title", ""),
                        "read_num": None,
                        "error": err,
                        "status": "failed"
                    })
                    task["failed"] += 1
                    consecutive_errors += 1
                    
                    if "过期" in err or consecutive_errors >= 10:
                        task["status"] = "stopped"
                        task["error"] = f"连续 {consecutive_errors} 次失败，任务已自动停止。原因: {err}"
                        with _reading_tasks_lock:
                            _reading_tasks[task_id] = {**task, "results": results}
                        return
                    
            except Exception as e:
                results.append({
                    "article_id": article["id"],
                    "title": article.get("title", ""),
                    "read_num": None,
                    "error": str(e),
                    "status": "error"
                })
                task["failed"] += 1
                consecutive_errors += 1
            
            task["processed"] = i + 1
            
            with _reading_tasks_lock:
                _reading_tasks[task_id] = {**task, "results": results}
            
            # 请求间隔
            if i < len(article_data) - 1:
                await asyncio.sleep(3)
        
        with _reading_tasks_lock:
            _reading_tasks[task_id]["status"] = "completed"
            _reading_tasks[task_id]["results"] = results
    
    asyncio.run(_run())


# ========== 查询接口 ==========

@router.get("/task/{task_id}", summary="查询批量获取任务状态")
async def get_reading_task_status(
    task_id: str,
    current_user: dict = Depends(get_current_user_or_ak)
):
    """查询批量获取阅读量任务的状态和进度"""
    task = _get_reading_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=error_response(40404, "任务不存在"))
    
    # 返回状态（不含完整结果列表，减少响应大小）
    summary = {**task}
    if len(summary.get("results", [])) > 20:
        summary["results"] = summary["results"][:20]
        summary["results_truncated"] = True
    
    return success_response(summary)


@router.get("/article/{article_id}", summary="获取单篇文章阅读量")
async def get_article_reading(
    article_id: str,
    current_user: dict = Depends(get_current_user_or_ak)
):
    """获取单篇文章的阅读量数据（从数据库读取已缓存的数据）"""
    session = DB.get_session()
    try:
        from core.models.article import Article
        article = session.query(Article).filter(Article.id == article_id).first()
        if not article:
            raise HTTPException(status_code=404, detail=error_response(40401, "文章不存在"))
        
        existing_stats = None
        try:
            publish_info = json.loads(article.publish_info) if article.publish_info else {}
            existing_stats = publish_info.get("reading_stats")
        except (json.JSONDecodeError, TypeError):
            pass
        
        if existing_stats:
            return success_response({
                "article_id": article_id,
                "title": article.title,
                "read_num": existing_stats.get("read_num"),
                "like_num": existing_stats.get("like_num"),
                "old_like_num": existing_stats.get("old_like_num"),
                "updated_at": existing_stats.get("updated_at"),
                "source": "cached"
            })
        else:
            return success_response({
                "article_id": article_id,
                "title": article.title,
                "read_num": None,
                "like_num": None,
                "old_like_num": None,
                "message": "暂无阅读量数据",
                "source": "none"
            })
            
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_201_CREATED,
            detail=error_response(50001, f"获取阅读量失败: {str(e)}")
        )


@router.get("/stats/{mp_id}", summary="获取公众号文章阅读量统计")
async def get_mp_reading_stats(
    mp_id: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: dict = Depends(get_current_user_or_ak)
):
    """获取指定公众号文章的阅读量统计列表"""
    session = DB.get_session()
    try:
        from core.models.article import Article
        from core.models.feed import Feed
        
        feed = session.query(Feed).filter(Feed.id == mp_id).first()
        if not feed:
            raise HTTPException(status_code=404, detail=error_response(40401, "公众号不存在"))
        
        articles = session.query(Article).filter(
            Article.mp_id == mp_id,
            Article.publish_info != None,
            Article.publish_info != "{}",
            Article.publish_info != ""
        ).order_by(Article.publish_time.desc()).limit(limit).offset(offset).all()
        
        stats = []
        for article in articles:
            try:
                publish_info = json.loads(article.publish_info) if article.publish_info else {}
                reading_stats = publish_info.get("reading_stats", {})
                if reading_stats:
                    stats.append({
                        "article_id": article.id,
                        "title": article.title,
                        "url": article.url,
                        "publish_time": article.publish_time,
                        "read_num": reading_stats.get("read_num"),
                        "like_num": reading_stats.get("like_num"),
                        "old_like_num": reading_stats.get("old_like_num"),
                        "updated_at": reading_stats.get("updated_at")
                    })
            except (json.JSONDecodeError, TypeError):
                continue
        
        return success_response({
            "mp_id": mp_id,
            "mp_name": feed.mp_name,
            "list": stats,
            "total": len(stats)
        })
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_201_CREATED,
            detail=error_response(50001, f"获取统计失败: {str(e)}")
        )


@router.get("/overview", summary="全局阅读量概览")
async def get_reading_overview(
    current_user: dict = Depends(get_current_user_or_ak)
):
    """获取所有公众号的阅读量概览统计"""
    session = DB.get_session()
    try:
        from core.models.article import Article
        from sqlalchemy import func
        
        articles_with_stats = session.query(Article).filter(
            Article.publish_info != None,
            Article.publish_info != "{}",
            Article.publish_info != "",
            Article.publish_info.like('%"reading_stats"%')
        ).all()
        
        total_read = 0
        total_like = 0
        total_old_like = 0
        count_with_data = 0
        
        for article in articles_with_stats:
            try:
                info = json.loads(article.publish_info)
                stats = info.get("reading_stats", {})
                if stats.get("read_num") is not None:
                    total_read += stats["read_num"] or 0
                    total_like += stats.get("like_num") or 0
                    total_old_like += stats.get("old_like_num") or 0
                    count_with_data += 1
            except (json.JSONDecodeError, TypeError):
                continue
        
        total_articles = session.query(func.count(Article.id)).scalar() or 0
        
        return success_response({
            "total_articles": total_articles,
            "articles_with_reading": count_with_data,
            "total_read_count": total_read,
            "total_like_count": total_like,
            "total_old_like_count": total_old_like,
            "avg_read_count": round(total_read / count_with_data) if count_with_data > 0 else 0
        })
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_201_CREATED,
            detail=error_response(50001, f"获取概览失败: {str(e)}")
        )
