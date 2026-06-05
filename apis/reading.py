"""
阅读量 API 模块
提供 Token 管理、文章阅读量获取和管理接口
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
    test_url: str = Body(..., description="任意一篇微信文章的URL，用于测试Token有效性"),
    current_user: dict = Depends(get_current_user_or_ak)
):
    """用一篇已知文章URL测试Token是否有效"""
    from core.wx_reading_fetcher import fetch_reading_count
    
    result = fetch_reading_count(test_url)
    
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
            "message": "Token 无效或已过期，请重新从 Fiddler 抓包获取。"
        })


# ========== 批量阅读量获取 ==========

@router.post("/batch", summary="批量获取公众号文章阅读量")
async def batch_get_reading(
    mp_id: str = Body(..., description="公众号ID"),
    max_articles: int = Body(100, description="最多获取文章数", ge=1, le=500),
    current_user: dict = Depends(get_current_user_or_ak)
):
    """
    批量获取指定公众号文章的阅读量（后台异步执行）
    
    需要先通过 /token 接口上传微信移动端 Token
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
            "status": "running",
            "results": [],
            "created_at": __import__('datetime').datetime.now().isoformat()
        })
        
        # 准备文章数据（在 session 关闭前取出）
        article_data = [
            {"id": a.id, "url": a.url, "title": a.title[:60]}
            for a in articles
        ]
        
        # 启动后台线程执行
        def run_batch_task():
            _run_batch_reading_task_sync(task_id, article_data)
        
        thread = threading.Thread(target=run_batch_task, daemon=True)
        thread.start()
        
        return success_response({
            "task_id": task_id,
            "mp_id": mp_id,
            "mp_name": feed.mp_name,
            "total_articles": len(articles),
            "status": "running",
            "message": f"已开始获取 {len(articles)} 篇文章的阅读量，通过 GET /api/v1/wx/reading/task/{{task_id}} 查看进度"
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
    
    会遍历所有已订阅的公众号，逐个获取文章阅读量。
    需要先通过 /token 接口上传微信移动端 Token。
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
    from core.wx_reading_fetcher import fetch_reading_count, save_reading_to_db
    
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
