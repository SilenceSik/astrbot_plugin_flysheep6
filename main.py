from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.filter.command import GreedyStr

from .flysheep import (
    GamePost,
    build_search_url,
    format_report,
    format_search_report,
    next_daily_run,
    parse_category_ids,
    parse_wordpress_post,
    split_report,
)


PLUGIN_NAME = "astrbot_plugin_flysheep6"
DEFAULT_API_URL = "https://www.flysheep6.com/wp-json/wp/v2/posts"


@register(
    PLUGIN_NAME,
    "chen",
    "每天完整推送 flysheep 最近三天游戏，并支持按关键词定向搜索。",
    "v1.1.0",
)
class Flysheep6Plugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._scheduler_task: asyncio.Task[None] | None = None
        self._next_run: datetime | None = None
        self._fetch_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._state_path = self._get_state_path()
        self._state = self._load_state()

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        if self._bool_config("enabled", True) and self._scheduler_task is None:
            self._scheduler_task = asyncio.create_task(
                self._scheduler_loop(), name="flysheep6-daily-push"
            )
            logger.info("[flysheep6] 每日定时任务已启动")
        elif not self._bool_config("enabled", True):
            logger.info("[flysheep6] 定时推送已在配置中关闭")

    @filter.command("避难所游戏")
    async def recent_games(self, event: AstrMessageEvent):
        """手动查询最近几天的游戏文章。"""
        try:
            posts = await self._fetch_recent_games()
            if not posts:
                yield event.plain_result(
                    f"flysheep 最近{self._days()}天没有找到新游戏。"
                )
                return
            for chunk in self._report_chunks(posts):
                yield event.plain_result(chunk)
        except Exception as exc:
            logger.exception("[flysheep6] 手动查询失败")
            yield event.plain_result(f"查询 flysheep 失败：{exc}")

    @filter.command("避难所搜索")
    async def search_games(self, event: AstrMessageEvent, keyword: GreedyStr):
        """按游戏名或关键词定向搜索站点文章。"""
        keyword = keyword.strip()
        if not keyword:
            yield event.plain_result("用法：/避难所搜索 游戏名")
            return
        try:
            posts = await self._fetch_search_games(keyword)
            if not posts:
                yield event.plain_result(
                    f"flysheep 没有找到“{keyword}”相关游戏。\n"
                    f"站内搜索：{build_search_url(keyword)}"
                )
                return
            for chunk in self._search_report_chunks(posts, keyword):
                yield event.plain_result(chunk)
        except Exception as exc:
            logger.exception("[flysheep6] 定向搜索失败：%s", keyword)
            yield event.plain_result(f"搜索 flysheep 失败：{exc}")

    @filter.command("避难所订阅")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def subscribe(self, event: AstrMessageEvent):
        """订阅当前会话的每日推送。"""
        target = event.unified_msg_origin
        targets = self._targets()
        if target in targets:
            yield event.plain_result("当前会话已经订阅 flysheep 游戏日报。")
            return
        targets.append(target)
        self.config["targets"] = targets
        self.config.save_config()
        logger.info("[flysheep6] 新增订阅会话：%s", target)
        yield event.plain_result(
            f"已订阅当前会话，每天 {self._push_time()} 推送最近"
            f"{self._days()}天完整游戏列表。"
        )

    @filter.command("避难所退订")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def unsubscribe(self, event: AstrMessageEvent):
        """取消当前会话的每日推送。"""
        target = event.unified_msg_origin
        targets = self._targets()
        if target not in targets:
            yield event.plain_result("当前会话没有订阅 flysheep 游戏日报。")
            return
        targets.remove(target)
        self.config["targets"] = targets
        self.config.save_config()
        logger.info("[flysheep6] 移除订阅会话：%s", target)
        yield event.plain_result("已取消当前会话的 flysheep 游戏日报。")

    @filter.command("避难所状态")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def status(self, event: AstrMessageEvent):
        """查看定时任务和订阅状态。"""
        enabled = self._bool_config("enabled", True)
        next_run = (
            self._next_run.strftime("%Y-%m-%d %H:%M %Z")
            if self._next_run
            else "尚未排定"
        )
        last_check = self._state.get("last_check_at") or "暂无"
        last_push = self._state.get("last_push_at") or "暂无"
        yield event.plain_result(
            "【flysheep 游戏日报状态】\n"
            f"定时推送：{'开启' if enabled else '关闭'}\n"
            f"订阅会话：{len(self._targets())} 个\n"
            f"查询范围：最近 {self._days()} 天\n"
            "推送模式："
            f"{'只推送未发送文章' if self._bool_config('only_new_on_schedule', False) else '每天完整推送'}\n"
            f"推送时间：{self._push_time()} ({self._timezone_name()})\n"
            f"下次运行：{next_run}\n"
            f"最近查询：{last_check}\n"
            f"最近推送：{last_push}"
        )

    async def terminate(self) -> None:
        if self._scheduler_task:
            self._scheduler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._scheduler_task
            self._scheduler_task = None
        logger.info("[flysheep6] 定时任务已停止")

    async def _scheduler_loop(self) -> None:
        while True:
            try:
                self._next_run = next_daily_run(
                    datetime.now(timezone.utc),
                    self._push_time(),
                    self._timezone_name(),
                )
            except Exception as exc:
                logger.error("[flysheep6] 推送时间配置无效：%s", exc)
                self._next_run = next_daily_run(
                    datetime.now(timezone.utc), "09:00", "Asia/Shanghai"
                )

            delay = max(
                1.0,
                (
                    self._next_run.astimezone(timezone.utc)
                    - datetime.now(timezone.utc)
                ).total_seconds(),
            )
            logger.info(
                "[flysheep6] 下次定时查询：%s",
                self._next_run.strftime("%Y-%m-%d %H:%M:%S %Z"),
            )
            await asyncio.sleep(delay)
            await self._run_scheduled_with_retries()

    async def _run_scheduled_with_retries(self) -> None:
        for attempt in range(1, 4):
            try:
                await self._push_to_subscribers()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[flysheep6] 定时查询失败（第 %d/3 次）", attempt)
                if attempt < 3:
                    await asyncio.sleep(300)

    async def _push_to_subscribers(self) -> None:
        targets = self._targets()
        if not targets:
            logger.info("[flysheep6] 没有订阅会话，本次只完成排程")
            return

        posts = await self._fetch_recent_games()
        changed = False
        failed_targets: list[str] = []
        for target in targets:
            try:
                target_posts = self._unsent_posts(target, posts)
                if not target_posts:
                    if self._bool_config("notify_empty", False):
                        await self._send_text(
                            target,
                            f"flysheep 最近{self._days()}天没有找到游戏。",
                        )
                    continue
                for chunk in self._report_chunks(target_posts):
                    await self._send_text(target, chunk)
                    await asyncio.sleep(1)
            except Exception:
                logger.exception("[flysheep6] 向订阅会话推送失败：%s", target)
                failed_targets.append(target)
                continue

            self._mark_sent(target, target_posts)
            changed = True
            logger.info(
                "[flysheep6] 已向 %s 推送 %d 款游戏", target, len(target_posts)
            )

        self._state["last_check_at"] = datetime.now(timezone.utc).isoformat()
        if changed:
            self._state["last_push_at"] = self._state["last_check_at"]
        await self._save_state()
        if failed_targets:
            raise RuntimeError(f"{len(failed_targets)} 个订阅会话推送失败")

    async def _fetch_recent_games(self) -> list[GamePost]:
        async with self._fetch_lock:
            days = self._days()
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            category_ids = parse_category_ids(
                self.config.get("category_ids", "4,5,32,72")
            )
            params = {
                "per_page": "100",
                "after": cutoff.isoformat().replace("+00:00", "Z"),
                "orderby": "date",
                "order": "desc",
                "_fields": (
                    "id,date_gmt,date,link,title,excerpt,content,categories"
                ),
            }
            if category_ids:
                params["categories"] = ",".join(map(str, category_ids))

            timeout = aiohttp.ClientTimeout(
                total=self._int_config("request_timeout", 20, 5, 60)
            )
            headers = {
                "Accept": "application/json",
                "User-Agent": "AstrBot-Flysheep6/1.0",
            }
            raw_posts: list[dict[str, Any]] = []
            total_pages = 1
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                for page in range(1, 4):
                    page_params = {**params, "page": str(page)}
                    async with session.get(self._api_url(), params=page_params) as response:
                        if response.status != 200:
                            body = (await response.text())[:300]
                            raise RuntimeError(
                                f"网站 API 返回 HTTP {response.status}: {body}"
                            )
                        try:
                            total_pages = min(
                                3, int(response.headers.get("X-WP-TotalPages", "1"))
                            )
                        except ValueError:
                            total_pages = 1
                        payload = await response.json(content_type=None)
                    if not isinstance(payload, list):
                        raise RuntimeError("网站 API 返回了非列表数据")
                    raw_posts.extend(item for item in payload if isinstance(item, dict))
                    if page >= total_pages or len(payload) < 100:
                        break

            posts: list[GamePost] = []
            parsed_ids: set[int] = set()
            timezone_name = self._timezone_name()
            intro_length = self._int_config("intro_length", 100, 40, 300)
            for raw in raw_posts:
                try:
                    post = parse_wordpress_post(
                        raw,
                        timezone_name,
                        intro_length,
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    logger.warning(
                        "[flysheep6] 跳过无法解析的文章 id=%s: %s",
                        raw.get("id"),
                        exc,
                    )
                    continue
                if (
                    post.post_id not in parsed_ids
                    and post.published_at.astimezone(timezone.utc) >= cutoff
                ):
                    posts.append(post)
                    parsed_ids.add(post.post_id)

            posts.sort(key=lambda item: item.published_at, reverse=True)
            return posts[: self._int_config("max_items", 30, 1, 100)]

    async def _fetch_search_games(self, keyword: str) -> list[GamePost]:
        async with self._fetch_lock:
            max_items = self._int_config("search_max_items", 10, 1, 30)
            params = {
                "per_page": str(max_items),
                "search": keyword,
                "orderby": "relevance",
                "_fields": (
                    "id,date_gmt,date,link,title,excerpt,content,categories"
                ),
            }
            category_ids = parse_category_ids(
                self.config.get("category_ids", "4,5,32,72")
            )
            if category_ids:
                params["categories"] = ",".join(map(str, category_ids))

            timeout = aiohttp.ClientTimeout(
                total=self._int_config("request_timeout", 20, 5, 60)
            )
            headers = {
                "Accept": "application/json",
                "User-Agent": "AstrBot-Flysheep6/1.1",
            }
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(self._api_url(), params=params) as response:
                    if response.status != 200:
                        body = (await response.text())[:300]
                        raise RuntimeError(
                            f"网站 API 返回 HTTP {response.status}: {body}"
                        )
                    payload = await response.json(content_type=None)
            if not isinstance(payload, list):
                raise RuntimeError("网站 API 返回了非列表数据")

            posts: list[GamePost] = []
            parsed_ids: set[int] = set()
            for raw in payload:
                if not isinstance(raw, dict):
                    continue
                try:
                    post = parse_wordpress_post(
                        raw,
                        self._timezone_name(),
                        self._int_config("intro_length", 100, 40, 300),
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    logger.warning(
                        "[flysheep6] 跳过无法解析的搜索结果 id=%s: %s",
                        raw.get("id"),
                        exc,
                    )
                    continue
                if post.post_id not in parsed_ids:
                    posts.append(post)
                    parsed_ids.add(post.post_id)
            return posts

    def _report_chunks(self, posts: list[GamePost]) -> list[str]:
        return split_report(
            format_report(posts, self._days()),
            self._int_config("max_message_chars", 3500, 500, 10000),
        )

    def _search_report_chunks(
        self, posts: list[GamePost], keyword: str
    ) -> list[str]:
        return split_report(
            format_search_report(posts, keyword),
            self._int_config("max_message_chars", 3500, 500, 10000),
        )

    def _unsent_posts(self, target: str, posts: list[GamePost]) -> list[GamePost]:
        if not self._bool_config("only_new_on_schedule", False):
            return posts
        stored_ids = self._state.get("sent_ids", {}).get(target, [])
        sent_ids = set(stored_ids) if isinstance(stored_ids, list) else set()
        return [post for post in posts if post.post_id not in sent_ids]

    def _mark_sent(self, target: str, posts: list[GamePost]) -> None:
        sent_by_target = self._state.setdefault("sent_ids", {})
        stored_ids = sent_by_target.get(target, [])
        existing: list[int] = []
        if isinstance(stored_ids, list):
            for item in stored_ids:
                try:
                    existing.append(int(item))
                except (TypeError, ValueError):
                    continue
        merged = list(dict.fromkeys([post.post_id for post in posts] + existing))
        sent_by_target[target] = merged[:500]

    async def _send_text(self, target: str, text: str) -> None:
        await self.context.send_message(target, MessageChain().message(text))

    def _get_state_path(self) -> Path:
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir / "state.json"

    def _load_state(self) -> dict[str, Any]:
        if not self._state_path.exists():
            return self._empty_state()
        try:
            loaded = json.loads(self._state_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                loaded.setdefault("version", 1)
                if not isinstance(loaded.get("sent_ids"), dict):
                    loaded["sent_ids"] = {}
                loaded.setdefault("last_check_at", None)
                loaded.setdefault("last_push_at", loaded.get("last_success_at"))
                return loaded
        except (OSError, json.JSONDecodeError):
            logger.exception("[flysheep6] 状态文件读取失败，将使用空状态")
        return self._empty_state()

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {
            "version": 1,
            "sent_ids": {},
            "last_check_at": None,
            "last_push_at": None,
        }

    async def _save_state(self) -> None:
        async with self._state_lock:
            temporary = self._state_path.with_suffix(".tmp")
            content = json.dumps(self._state, ensure_ascii=False, indent=2)
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(self._state_path)

    def _targets(self) -> list[str]:
        configured = self.config.get("targets", [])
        if not isinstance(configured, list):
            return []
        return list(
            dict.fromkeys(
                item.strip()
                for item in configured
                if isinstance(item, str) and item.strip()
            )
        )

    def _api_url(self) -> str:
        return str(self.config.get("api_url", DEFAULT_API_URL)).strip() or DEFAULT_API_URL

    def _push_time(self) -> str:
        configured = str(self.config.get("push_time", "09:00")).strip()
        try:
            hour_text, minute_text = configured.split(":", maxsplit=1)
            hour, minute = int(hour_text), int(minute_text)
            if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                raise ValueError
        except (TypeError, ValueError):
            logger.warning("[flysheep6] 无效推送时间 %r，使用 09:00", configured)
            return "09:00"
        return f"{hour:02d}:{minute:02d}"

    def _timezone_name(self) -> str:
        configured = str(self.config.get("timezone", "Asia/Shanghai")).strip()
        try:
            ZoneInfo(configured)
        except (ValueError, ZoneInfoNotFoundError):
            logger.warning(
                "[flysheep6] 无效时区 %r，使用 Asia/Shanghai", configured
            )
            return "Asia/Shanghai"
        return configured

    def _days(self) -> int:
        return self._int_config("days", 3, 1, 14)

    def _bool_config(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _int_config(self, key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default
        return max(minimum, min(value, maximum))
