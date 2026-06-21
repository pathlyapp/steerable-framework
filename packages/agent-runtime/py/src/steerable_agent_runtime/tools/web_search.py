"""WebSearch helper using DuckDuckGo and BeautifulSoup.

Uses a lightweight LLM optimization call to produce an optimized search query
and region before hitting DuckDuckGo. Fetches the top search results in parallel
and parses HTML to extract text content, complete with safety filtering.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

SEARCH_TIMEOUT_SECONDS = 15
DEFAULT_MAX_RESULTS = 5
DEEP_FETCH_TOP_N = 3
DEEP_FETCH_TIMEOUT = 8
DEEP_FETCH_MAX_CHARS = 1500
SELF_DOMAINS = set(filter(None, os.environ.get("STEERABLE_SELF_DOMAINS", "").split(",")))

_NSFW_KEYWORDS = {
    "porn", "xxx", "sex", "hentai", "nude", "naked", "nsfw",
    "adult video", "adult film", "erotic", "pornhub", "xvideos",
    "xnxx", "xhamster", "redtube", "youporn", "brazzers", "onlyfans",
    "camgirl", "webcam sex", "escort", "hookup",
    "色情", "黄色", "成人视频", "成人网站", "成人内容", "裸体", "裸照",
    "做爱", "性爱", "约炮", "一夜情", "叫床", "口交", "肛交",
    "自慰", "手淫", "AV女优", "番号", "无码", "有码", "中出",
    "颜射", "潮吹", "三级片", "毛片", "黄片", "A片",
    "情色", "风俗", "援交", "卖淫", "嫖娼",
}

_NSFW_DOMAIN_KEYWORDS = {
    "porn", "xxx", "sex", "hentai", "nude", "nsfw", "adult",
    "xvideos", "xnxx", "xhamster", "redtube", "youporn", "pornhub",
    "brazzers", "onlyfans", "chaturbate", "livejasmin",
    "spankbang", "tube8", "beeg",
}


def _is_nsfw_text(text: str) -> bool:
    """Check if text contains NSFW keywords."""
    lower = text.lower()
    return any(kw in lower for kw in _NSFW_KEYWORDS)


def _is_nsfw_url(url: str) -> bool:
    """Check if a URL belongs to a known adult domain."""
    lower = url.lower()
    try:
        domain = lower.split("//", 1)[1].split("/")[0]
    except (IndexError, ValueError):
        return False
    return any(kw in domain for kw in _NSFW_DOMAIN_KEYWORDS)


def _filter_nsfw_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove search results that look like adult content."""
    filtered = []
    for r in results:
        href = r.get("href", "")
        title = r.get("title", "")
        body = r.get("body", "")
        if _is_nsfw_url(href):
            logger.info("nsfw_filter_blocked url=%s reason=domain", href[:80])
            continue
        if _is_nsfw_text(title) or _is_nsfw_text(body):
            logger.info("nsfw_filter_blocked url=%s reason=content", href[:80])
            continue
        filtered.append(r)
    return filtered


def _extract_host(url: str) -> str:
    """Extract host from URL in lower-case."""
    try:
        parsed = urlparse(url)
        return (parsed.hostname or "").lower()
    except Exception:
        return ""


def _normalize_domains(exclude_domains: list[str] | None) -> set[str]:
    """Normalize exclusion domains."""
    normalized = {domain.lower().strip(".") for domain in SELF_DOMAINS if domain}
    for item in exclude_domains or []:
        if not isinstance(item, str):
            continue
        candidate = item.strip().lower()
        if not candidate:
            continue
        parsed = urlparse(candidate if "://" in candidate else f"https://{candidate}")
        host = (parsed.hostname or parsed.path or "").strip().lower().strip(".")
        if host:
            normalized.add(host)
    return normalized


def _is_blocked_host(host: str, blocked_domains: set[str]) -> bool:
    """Check whether host is blocked."""
    host = host.lower().strip(".")
    if not host:
        return False
    for blocked in blocked_domains:
        if host == blocked or host.endswith(f".{blocked}"):
            return True
    return False


def _filter_blocked_domains(
    results: list[dict[str, Any]],
    exclude_domains: list[str] | None,
) -> list[dict[str, Any]]:
    """Remove results whose host matches blocked domains."""
    blocked_domains = _normalize_domains(exclude_domains)
    filtered: list[dict[str, Any]] = []
    for result in results:
        href = result.get("href", "")
        host = _extract_host(href)
        if _is_blocked_host(host, blocked_domains):
            logger.info("domain_filter_blocked url=%s host=%s", href[:120], host)
            continue
        filtered.append(result)
    return filtered


_NSFW_REVIEW_SYSTEM_PROMPT = """\
你是一个内容安全审核员。你的任务是判断搜索结果中是否包含不适当内容。

不适当内容包括但不限于：
- 色情、成人、裸露相关内容
- 暴力、血腥内容
- 赌博、诈骗相关内容
- 违禁药品相关内容
- 任何形式的擦边球或暗示性成人内容

对每条搜索结果，判断是否安全。只返回 JSON，格式为：
{"safe_indices": [0, 1, 3]}
其中 safe_indices 是所有安全结果的索引号列表（从0开始）。
如果全部安全则返回所有索引，如果全部不安全则返回空列表。"""

_NSFW_REVIEW_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "nsfw_review",
        "schema": {
            "type": "object",
            "properties": {
                "safe_indices": {
                    "type": "array",
                    "items": {"type": "integer"},
                },
            },
            "required": ["safe_indices"],
            "additionalProperties": False,
        },
    },
}

_SEARCH_PARAM_SYSTEM_PROMPT = """\
你是一个搜索查询优化器。根据用户的消息和对话上下文，生成最优的网络搜索参数。

规则：
1. query：生成简洁、精准的搜索关键词，去掉口语化表达，保留核心实体和意图
2. region：根据搜索内容的地理相关性选择区域代码
   - cn-zh：中国相关内容（天气、新闻、地点、中文网站等）
   - us-en：英文/国际内容
   - jp-jp：日本相关内容
   - wt-wt：无特定地区偏好
3. 如果提供了用户位置信息，将位置的城市/地区名融入搜索关键词中（比如"天气"→"北京天气"）
4. 严禁生成任何色情、成人、暴力、违法相关的搜索词。如果用户意图涉及此类内容，将查询改写为安全的替代表述或拒绝生成。

只返回 JSON，不要有其他内容。"""

_SEARCH_PARAM_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "search_params",
        "schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "优化后的搜索关键词",
                },
                "region": {
                    "type": "string",
                    "enum": ["cn-zh", "us-en", "jp-jp", "wt-wt"],
                    "description": "搜索区域代码",
                },
            },
            "required": ["query", "region"],
            "additionalProperties": False,
        },
    },
}


async def _generate_search_params(
    message: str,
    chat_history: list[dict[str, str]] | None = None,
    location: dict | None = None,
    client: Any | None = None,
) -> dict[str, str]:
    """Use a lightweight LLM call to produce an optimized search query + region."""
    fallback = {"query": message, "region": "wt-wt"}

    user_content = ""
    if location:
        loc_parts = []
        for key in ("country", "province", "city", "district"):
            val = location.get(key)
            if val:
                loc_parts.append(val)
        if loc_parts:
            user_content += f"用户当前位置: {' '.join(loc_parts)}\n"
    if chat_history:
        recent = chat_history[-6:]
        for turn in recent:
            role_label = "用户" if turn.get("role") == "user" else "助手"
            content = (turn.get("content") or "")[:200]
            user_content += f"{role_label}: {content}\n"
        user_content += "\n"
    user_content += f"当前用户消息: {message}"

    if client is None:
        try:
            import os
            from openai import AsyncOpenAI
            api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("SILICONFLOW_API_KEY")
            base_url = os.environ.get("OPENAI_BASE_URL") or os.environ.get("SILICONFLOW_API_URL") or "https://api.openai.com/v1"
            if api_key:
                client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        except Exception:
            pass

    if client is None:
        return fallback

    try:
        model = os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"
        try:
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": _SEARCH_PARAM_SYSTEM_PROMPT},
                        {"role": "user", "content": user_content},
                    ],
                    temperature=0,
                    max_completion_tokens=150,
                    response_format=_SEARCH_PARAM_SCHEMA,
                ),
                timeout=8,
            )
        except Exception:
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": _SEARCH_PARAM_SYSTEM_PROMPT},
                        {"role": "user", "content": user_content},
                    ],
                    temperature=0,
                    max_completion_tokens=150,
                    response_format={"type": "json_object"},
                ),
                timeout=8,
            )

        raw = (response.choices[0].message.content or "").strip()
        params = json.loads(raw)
        query = str(params.get("query") or message).strip()
        region = str(params.get("region") or "wt-wt").strip()
        if region not in ("cn-zh", "us-en", "jp-jp", "wt-wt"):
            region = "wt-wt"

        return {"query": query, "region": region}
    except Exception as e:
        logger.warning("web_search_params_fallback error=%s", e)
        return fallback


async def _llm_filter_nsfw_results(
    results: list[dict[str, Any]],
    client: Any | None = None,
) -> list[dict[str, Any]]:
    """Use lightweight LLM to filter NSFW results."""
    if not results:
        return results

    if client is None:
        try:
            import os
            from openai import AsyncOpenAI
            api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("SILICONFLOW_API_KEY")
            base_url = os.environ.get("OPENAI_BASE_URL") or os.environ.get("SILICONFLOW_API_URL") or "https://api.openai.com/v1"
            if api_key:
                client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        except Exception:
            pass

    if client is None:
        return results

    review_lines = []
    for i, r in enumerate(results):
        title = r.get("title", "")[:100]
        body = r.get("body", "")[:200]
        href = r.get("href", "")[:100]
        review_lines.append(f"[{i}] 标题: {title}\n    摘要: {body}\n    URL: {href}")
    user_content = "\n\n".join(review_lines)

    try:
        model = os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"
        try:
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": _NSFW_REVIEW_SYSTEM_PROMPT},
                        {"role": "user", "content": user_content},
                    ],
                    temperature=0,
                    max_completion_tokens=200,
                    response_format=_NSFW_REVIEW_SCHEMA,
                ),
                timeout=6,
            )
        except Exception:
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": _NSFW_REVIEW_SYSTEM_PROMPT},
                        {"role": "user", "content": user_content},
                    ],
                    temperature=0,
                    max_completion_tokens=200,
                    response_format={"type": "json_object"},
                ),
                timeout=6,
            )

        raw = (response.choices[0].message.content or "").strip()
        review = json.loads(raw)
        safe_indices = set(review.get("safe_indices", []))

        filtered = []
        for i, r in enumerate(results):
            if i in safe_indices:
                filtered.append(r)
        return filtered
    except Exception as e:
        logger.warning("nsfw_llm_filter_error error=%s (fail-open)", e)
        return results


async def _run_search(query: str, region: str, max_results: int) -> list[dict[str, Any]]:
    """Execute DuckDuckGo search in a thread pool."""
    from ddgs import DDGS

    def _sync_search() -> list[dict[str, Any]]:
        with DDGS() as ddgs:
            return ddgs.text(query, region=region, safesearch="on", max_results=max_results)

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_search)


def _extract_sources(results: list[dict]) -> list[dict[str, str]]:
    sources: list[dict[str, str]] = []
    for r in results:
        href = r.get("href", "")
        if not href:
            continue
        try:
            domain = href.split("//", 1)[1].split("/")[0]
            scheme = href.split("//", 1)[0]
        except Exception:
            domain = ""
            scheme = "https:"
        favicon = f"{scheme}//{domain}/favicon.ico" if domain else ""
        sources.append({
            "title": r.get("title", ""),
            "url": href,
            "favicon": favicon,
        })
    return sources


async def _fetch_single_page(
    client: httpx.AsyncClient,
    url: str,
    blocked_domains: set[str],
) -> tuple[str, str]:
    try:
        source_host = _extract_host(url)
        if _is_blocked_host(source_host, blocked_domains):
            return url, ""

        resp = await client.get(url, follow_redirects=True, timeout=DEEP_FETCH_TIMEOUT)
        final_host = _extract_host(str(resp.url))
        if _is_blocked_host(final_host, blocked_domains):
            return url, ""
        if resp.status_code != 200:
            return url, ""

        content_type = resp.headers.get("content-type", "")
        if "text/html" not in content_type and "text/plain" not in content_type:
            return url, ""

        html = resp.text
        text = _extract_text_from_html(html)
        return url, text
    except Exception:
        return url, ""


def _extract_text_from_html(html: str) -> str:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "nav", "footer", "header", "aside",
                     "noscript", "iframe", "form", "button", "svg", "meta",
                     "link"]):
        tag.decompose()

    main = soup.find("article") or soup.find("main") or soup.find("div", {"role": "main"})
    target = main if main else soup.body if soup.body else soup

    lines: list[str] = []
    for text in target.stripped_strings:
        line = text.strip()
        if line:
            lines.append(line)

    full_text = "\n".join(lines)
    full_text = re.sub(r"\n{3,}", "\n\n", full_text)

    if len(full_text) > DEEP_FETCH_MAX_CHARS:
        full_text = full_text[:DEEP_FETCH_MAX_CHARS] + "..."

    return full_text


async def _deep_fetch_pages(
    results: list[dict],
    exclude_domains: list[str] | None = None,
) -> dict[str, str]:
    urls = [r.get("href", "") for r in results if r.get("href")]
    if not urls:
        return {}
    blocked_domains = _normalize_domains(exclude_domains)
    urls = [url for url in urls if not _is_blocked_host(_extract_host(url), blocked_domains)]
    if not urls:
        return {}

    page_contents: dict[str, str] = {}
    try:
        async with httpx.AsyncClient(
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
            verify=False,
        ) as client:
            tasks = [_fetch_single_page(client, url, blocked_domains) for url in urls]
            done = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=DEEP_FETCH_TIMEOUT + 2,
            )
            for item in done:
                if isinstance(item, tuple) and len(item) == 2:
                    url, text = item
                    if text:
                        page_contents[url] = text
    except Exception as e:
        logger.warning("deep_fetch_batch_error error=%s", e)

    return page_contents


def _format_results(results: list[dict], page_contents: dict[str, str] | None = None) -> str:
    if page_contents is None:
        page_contents = {}

    lines = ["[联网搜索结果]", ""]
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        body = r.get("body", "")
        href = r.get("href", "")
        lines.append(f"{i}. {title}")
        if body:
            lines.append(f"   {body}")
        if href:
            lines.append(f"   链接: {href}")

        deep_text = page_contents.get(href, "")
        if deep_text:
            lines.append(f"   【网页正文摘要】")
            for dl in deep_text.split("\n"):
                lines.append(f"   {dl}")
        lines.append("")

    lines.append("请基于以上搜索结果回答用户的问题，并在适当位置引用来源。如有网页正文摘要，优先从中提取准确信息。")
    return "\n".join(lines)


async def web_search_with_sources(
    message: str,
    max_results: int = DEFAULT_MAX_RESULTS,
    chat_history: list[dict[str, str]] | None = None,
    location: dict | None = None,
    deep_fetch: bool = True,
    exclude_domains: list[str] | None = None,
    client: Any | None = None,
) -> tuple[Optional[str], list[dict[str, str]]]:
    """Search DuckDuckGo and return a tuple of (formatted_text, sources)."""
    try:
        params = await _generate_search_params(
            message, chat_history, location=location, client=client
        )
        query = params["query"]
        region = params["region"]

        results = await asyncio.wait_for(
            _run_search(query, region, max_results),
            timeout=SEARCH_TIMEOUT_SECONDS,
        )
        if not results:
            return None, []

        results = _filter_blocked_domains(results, exclude_domains)
        if not results:
            return None, []

        results = _filter_nsfw_results(results)
        if not results:
            return None, []

        results = await _llm_filter_nsfw_results(results, client=client)
        if not results:
            return None, []

        page_contents: dict[str, str] = {}
        if deep_fetch:
            page_contents = await _deep_fetch_pages(
                results[:DEEP_FETCH_TOP_N],
                exclude_domains=exclude_domains,
            )
            page_contents = {
                url: text for url, text in page_contents.items()
                if not _is_nsfw_text(text)
            }

        sources = _extract_sources(results)
        return _format_results(results, page_contents), sources

    except Exception as e:
        logger.error("web_search_error message=%s error=%s", message[:80], e, exc_info=True)
        return None, []


async def web_search(query: str, max_results: int = 5) -> str:
    """Search the internet for the given query and return a formatted summary of search results."""
    text, _ = await web_search_with_sources(query, max_results=max_results)
    return text or "未找到相关搜索结果。"

