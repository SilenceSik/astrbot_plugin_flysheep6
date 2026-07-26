from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from html.parser import HTMLParser
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


CATEGORY_NAMES = {
    4: "PC单机大作",
    5: "VR游戏",
    32: "小游戏/独立游戏",
    72: "模拟器整合游戏",
}

HTTP_HEADERS = {
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/138.0.0.0 Safari/537.36"
    ),
}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag in {"br", "div", "li", "p"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"div", "li", "p"}:
            self.parts.append(" ")


@dataclass(frozen=True, slots=True)
class GamePost:
    post_id: int
    title: str
    link: str
    published_at: datetime
    directories: tuple[str, ...]
    intro: str


def html_to_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value or "")
    parser.close()
    text = html.unescape("".join(parser.parts)).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def clean_intro(excerpt_html: str, content_html: str, limit: int) -> str:
    text = html_to_text(excerpt_html) or html_to_text(content_html)
    was_truncated = bool(re.search(r"\s*\[(?:…|\.\.\.)?\]\s*$", text))
    text = re.sub(r"\s*\[(?:…|\.\.\.|&hellip;)?\]\s*$", "", text)
    configuration = re.search(r"\s*(?:最低配置|推荐配置)\s*[:：]", text)
    if configuration:
        text = text[: configuration.start()]
        was_truncated = False
    text = text.strip(" \t\r\n-—")
    if not text:
        return "站点暂未提供简介"
    if len(text) <= limit:
        return f"{text}…" if was_truncated else text
    shortened = text[:limit].rstrip(" ，。！？；：,.!?;:")
    return f"{shortened}…"


def parse_wordpress_datetime(value: str, timezone_name: str) -> datetime:
    if not value:
        raise ValueError("文章缺少发布时间")
    normalized = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
    return parsed.astimezone(ZoneInfo(timezone_name))


def parse_wordpress_post(
    raw: dict[str, Any], timezone_name: str, intro_limit: int
) -> GamePost:
    title = html_to_text(str(raw.get("title", {}).get("rendered", "")))
    link = str(raw.get("link", "")).strip()
    if not title or not link:
        raise ValueError("文章缺少标题或链接")

    category_ids = raw.get("categories", [])
    directories = tuple(
        CATEGORY_NAMES[category_id]
        for category_id in category_ids
        if category_id in CATEGORY_NAMES
    ) or ("游戏",)

    date_gmt = str(raw.get("date_gmt") or "").strip()
    if date_gmt:
        published_at = parse_wordpress_datetime(date_gmt, timezone_name)
    else:
        local_date = str(raw.get("date") or "").strip()
        if not local_date:
            raise ValueError("文章缺少发布时间")
        published_at = datetime.fromisoformat(local_date)
        local_zone = ZoneInfo(timezone_name)
        published_at = (
            published_at.replace(tzinfo=local_zone)
            if published_at.tzinfo is None
            else published_at.astimezone(local_zone)
        )

    return GamePost(
        post_id=int(raw["id"]),
        title=title,
        link=link,
        published_at=published_at,
        directories=directories,
        intro=clean_intro(
            str(raw.get("excerpt", {}).get("rendered", "")),
            str(raw.get("content", {}).get("rendered", "")),
            intro_limit,
        ),
    )


def parse_category_ids(value: object) -> list[int]:
    if isinstance(value, (list, tuple)):
        parts: Iterable[object] = value
    else:
        parts = str(value or "").split(",")

    result: list[int] = []
    for part in parts:
        try:
            category_id = int(str(part).strip())
        except (TypeError, ValueError):
            continue
        if category_id > 0 and category_id not in result:
            result.append(category_id)
    return result


def fetch_wordpress_page(
    api_url: str, params: dict[str, str], timeout: int
) -> tuple[Any, dict[str, str]]:
    separator = (
        "" if api_url.endswith(("?", "&")) else ("&" if "?" in api_url else "?")
    )
    request = Request(
        f"{api_url}{separator}{urlencode(params)}",
        headers=HTTP_HEADERS,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200))
            body = response.read()
            response_headers = dict(response.headers.items())
    except HTTPError as exc:
        body = exc.read(300).decode("utf-8", errors="replace")
        raise RuntimeError(f"网站 API 返回 HTTP {exc.code}: {body}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"网站 API 请求失败：{exc}") from exc

    if status != 200:
        preview = body[:300].decode("utf-8", errors="replace")
        raise RuntimeError(f"网站 API 返回 HTTP {status}: {preview}")
    try:
        return json.loads(body.decode("utf-8-sig")), response_headers
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("网站 API 返回了无法解析的 JSON") from exc


def next_daily_run(now: datetime, push_time: str, timezone_name: str) -> datetime:
    try:
        hour_text, minute_text = push_time.strip().split(":", maxsplit=1)
        hour, minute = int(hour_text), int(minute_text)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("推送时间必须是 HH:MM 格式") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("推送时间超出有效范围")

    zone = ZoneInfo(timezone_name)
    local_now = now.astimezone(zone)
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate


def format_report(posts: list[GamePost], days: int) -> str:
    ordered = sorted(posts, key=lambda item: item.published_at, reverse=True)
    lines = [f"【flysheep 最近{days}天游戏】共 {len(ordered)} 款"]
    current_date = None
    for index, post in enumerate(ordered, start=1):
        published_date = post.published_at.date()
        if published_date != current_date:
            if lines[-1]:
                lines.append("")
            lines.append(post.published_at.strftime("%Y-%m-%d"))
            current_date = published_date
        directory = "/".join(post.directories)
        lines.extend(
            (
                f"{index}. [{directory}] {post.title}",
                f"简介：{post.intro}",
                f"链接：{post.link}",
                "",
            )
        )
    return "\n".join(lines).rstrip()


def build_search_url(
    keyword: str, site_url: str = "https://www.flysheep6.com/"
) -> str:
    return f"{site_url.rstrip('/')}/?{urlencode({'s': keyword.strip()})}"


def format_search_report(posts: list[GamePost], keyword: str) -> str:
    lines = [f"【flysheep 定向搜索】{keyword}（{len(posts)} 款）", ""]
    for index, post in enumerate(posts, start=1):
        directory = "/".join(post.directories)
        lines.extend(
            (
                f"{index}. [{directory}] {post.title}",
                f"发布：{post.published_at:%Y-%m-%d}",
                f"简介：{post.intro}",
                f"链接：{post.link}",
                "",
            )
        )
    lines.append(f"站内搜索：{build_search_url(keyword)}")
    return "\n".join(lines).rstrip()


def split_report(report: str, max_chars: int) -> list[str]:
    if len(report) <= max_chars:
        return [report]

    chunks: list[str] = []
    current = ""
    for paragraph in report.split("\n\n"):
        candidate = paragraph if not current else f"{current}\n\n{paragraph}"
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = paragraph
        while len(current) > max_chars:
            chunks.append(current[:max_chars])
            current = current[max_chars:]
    if current:
        chunks.append(current)
    return chunks
