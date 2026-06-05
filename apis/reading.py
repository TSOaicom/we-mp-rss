"""
阅读量 API 模块
提供文章阅读量的获取和管理接口
"""
import threading
import json
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


@router.get("/article/{article_id}", summary="获取单篇文章阅读量")
async def get_article_reading(
    article_id: str,
    current_user: dict = Depends(get_current_user_or_ak)
):
    """获取单篇文章的阅读量数据"""
    session = DB.get_session()
    try:
        from core.models.article import Article
        article = session.query(Article).filter(Article.id == article_id).first()
        if not article:
            raise HTTPException(status_code=404, detail=error_response(40401, "文章不存在"))
        
        if not article.url:
            return error_response(40001, "文章无URL，无法获取阅读量")
        
        # 检查是否已有阅读量数据
        existing_stats = None
        try:
            publish_info = json.loads(article.publish_info) if article.publish_info else {}
            existing_stats = publish_info.get("reading_stats")
        except (json.JSONDecodeError, TypeError):
            pass
        
        # 异步获取阅读量
        import asyncio
        from core.reading_count import capture_reading_count, save_reading_counts_to_db
        
        reading_data = await capture_reading_count(article.url)
        
        if reading_data.get("read_num") is not None:
            save_reading_counts_to_db(article_id, reading_data)
            return success_response({
                "article_id": article_id,
                "title": article.title,
                "read_num": reading_data.get("read_num"),
                "like_num": reading_data.get("like_num"),
                "old_like_num": reading_data.get("old_like_num"),
                "source": "live"
            })
        elif existing_stats:
            return success_response({
                "article_id": article_id,
                "title": article.title,
                "read_num": existing_stats.get("read_num"),
                "like_num": existing_stats.get("like_num"),
                "old_like_num": existing_stats.get("old_like_num"),
                "source": "cached",
                "updated_at": existing_stats.get("updated_at")
            })
        else:
            return success_response({
                "article_id": article_id,
                "title": article.title,
                "read_num": None,
                "like_num": None,
                "old_like_num": None,
                "message": reading_data.get("error", "未能获取阅读量"),
                "source": "none"
            })
            
    except HTTPException:
        raise
    except Exception as e:
        print(f"获取阅读量错误: {e}")
        raise HTTPException(
            status_code=status.HTTP_201_CREATED,
            detail=error_response(50001, f"获取阅读量失败: {str(e)}")
        )


@router.post("/batch", summary="批量获取公众号文章阅读量")
async def batch_get_reading(
    mp_id: str = Body(..., description="公众号ID"),
    max_articles: int = Body(20, description="最多获取文章数", ge=1, le=100),
    current_user: dict = Depends(get_current_user_or_ak)
):
    """批量获取指定公众号文章的阅读量（后台异步执行）"""
    import uuid
    
    session = DB.get_session()
    try:
        from core.models.article import Article
        from core.models.feed import Feed
        
        # 检查公众号是否存在
        feed = session.query(Feed).filter(Feed.id == mp_id).first()
        if not feed:
            raise HTTPException(status_code=404, detail=error_response(40401, "公众号不存在"))
        
        # 获取最近的文章（没有阅读量数据的优先）
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
        
        # 启动后台线程执行
        def run_batch_task():
            import asyncio
            asyncio.run(_run_batch_reading_task(task_id, articles))
        
        thread = threading.Thread(target=run_batch_task, daemon=True)
        thread.start()
        
        return success_response({
            "task_id": task_id,
            "mp_id": mp_id,
            "mp_name": feed.mp_name,
            "total_articles": len(articles),
            "status": "running",
            "message": f"已开始获取 {len(articles)} 篇文章的阅读量"
        })
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_201_CREATED,
            detail=error_response(50001, f"启动批量获取失败: {str(e)}")
        )


async def _run_batch_reading_task(task_id: str, articles):
    """后台执行批量阅读量获取"""
    from core.reading_count import capture_reading_count, save_reading_counts_to_db
    
    task = _get_reading_task(task_id)
    results = []
    
    for i, article in enumerate(articles):
        try:
            reading_data = await capture_reading_count(article.url, timeout=15000)
            
            if reading_data.get("read_num") is not None:
                save_reading_counts_to_db(article.id, reading_data)
                results.append({
                    "article_id": article.id,
                    "title": article.title[:50],
                    "read_num": reading_data["read_num"],
                    "like_num": reading_data.get("like_num"),
                    "status": "success"
                })
                task["success"] += 1
            else:
                results.append({
                    "article_id": article.id,
                    "title": article.title[:50],
                    "read_num": None,
                    "error": reading_data.get("error", "未知"),
                    "status": "failed"
                })
                task["failed"] += 1
                
        except Exception as e:
            results.append({
                "article_id": article.id,
                "title": article.title[:50],
                "read_num": None,
                "error": str(e),
                "status": "error"
            })
            task["failed"] += 1
        
        task["processed"] = i + 1
        
        # 更新任务状态
        with _reading_tasks_lock:
            _reading_tasks[task_id] = {**task, "results": results}
        
        # 请求间隔，避免频率限制
        if i < len(articles) - 1:
            import asyncio as _asyncio
            await _asyncio.sleep(5)
    
    # 完成
    with _reading_tasks_lock:
        _reading_tasks[task_id]["status"] = "completed"
        _reading_tasks[task_id]["results"] = results


@router.get("/task/{task_id}", summary="查询批量获取任务状态")
async def get_reading_task_status(
    task_id: str,
    current_user: dict = Depends(get_current_user_or_ak)
):
    """查询批量获取阅读量任务的状态"""
    task = _get_reading_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=error_response(40404, "任务不存在"))
    return success_response(task)


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
        from core.models.feed import Feed
        from sqlalchemy import func
        
        # 获取有阅读量数据的文章统计
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
        
        # 总文章数
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
