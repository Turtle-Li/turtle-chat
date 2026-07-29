"""Safe Markdown rendering for administrator-authored user announcements."""

from __future__ import annotations

from markdown_it import MarkdownIt


ANNOUNCEMENT_TITLE_MAX = 120
ANNOUNCEMENT_BODY_MAX = 20_000


_MARKDOWN = (
    MarkdownIt(
        "commonmark",
        {
            "html": False,
            "linkify": False,
            "typographer": False,
            "breaks": True,
            "maxNesting": 20,
        },
    )
    .enable("table")
    .enable("strikethrough")
    # Remote images can be tracking pixels. Image Markdown is rendered as a
    # normal link while headings, lists, tables, code, quotes and links remain.
    .disable("image")
)


def normalize_announcement(
    title: str,
    body_markdown: str,
    *,
    enabled: bool,
) -> tuple[str, str, bool]:
    normalized_title = str(title or "").strip()
    normalized_body = str(body_markdown or "").strip()
    normalized_enabled = bool(enabled)
    if len(normalized_title) > ANNOUNCEMENT_TITLE_MAX:
        raise ValueError(f"公告标题不能超过 {ANNOUNCEMENT_TITLE_MAX} 个字符")
    if len(normalized_body) > ANNOUNCEMENT_BODY_MAX:
        raise ValueError(f"公告正文不能超过 {ANNOUNCEMENT_BODY_MAX} 个字符")
    if not normalized_title:
        raise ValueError("保存公告前必须填写标题")
    if normalized_enabled and not normalized_body:
        raise ValueError("启用公告前必须填写 Markdown 正文")
    return normalized_title, normalized_body, normalized_enabled


def render_announcement_markdown(body_markdown: str) -> str:
    """Render CommonMark without raw HTML, linkification or remote images."""

    normalized = str(body_markdown or "").strip()
    if not normalized:
        return ""
    if len(normalized) > ANNOUNCEMENT_BODY_MAX:
        raise ValueError(f"公告正文不能超过 {ANNOUNCEMENT_BODY_MAX} 个字符")
    return _MARKDOWN.render(normalized)
