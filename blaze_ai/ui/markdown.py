"""A deliberately small Markdown → rich-text renderer for chat bubbles."""

from __future__ import annotations

import html
import re

from ..theme import Theme

_CODE_TOKEN = "@@BLAZE_CODE_BLOCK_{}@@"
_INLINE_TOKEN = "@@BLAZE_INLINE_CODE_{}@@"
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_BARE_LINK_RE = re.compile(r"(?<!\()(https?://[^\s<]+)")
_FENCE_RE = re.compile(r"```([^\n`]*)\n(.*?)```", flags=re.S)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_MATH_BLOCK_RE = re.compile(r"\$\$(.+?)\$\$", flags=re.S)
_MATH_INLINE_RE = re.compile(r"(?<!\$)\$([^\n$]+?)\$(?!\$)")
_MATHISH_RE = re.compile(r"[\\^_{}=+*/<>]|[A-Za-z]{2,}|\\[A-Za-z]+")
_FRAC_RE = re.compile(r"\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
_SQRT_RE = re.compile(r"\\sqrt\s*\{([^{}]*)\}")
_SUP_BRACE_RE = re.compile(r"\^\{([^{}]+)\}")
_SUB_BRACE_RE = re.compile(r"_\{([^{}]+)\}")
_SUP_RE = re.compile(r"\^([A-Za-z0-9+\-=()])")
_SUB_RE = re.compile(r"_([A-Za-z0-9+\-=()])")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", flags=re.S)
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_UNDERLINE_RE = re.compile(r"__(.+?)__", flags=re.S)
_BULLET_RE = re.compile(r"^(?:[-*•])\s+(.*)$")
_CONTROL_RE = re.compile(r"[\x00-\x07\x0b\x0e-\x1f\x7f]")

_MATH_SYMBOLS = {
    "\\times": "×",
    "\\cdot": "·",
    "\\div": "÷",
    "\\pm": "±",
    "\\mp": "∓",
    "\\leq": "≤",
    "\\geq": "≥",
    "\\neq": "≠",
    "\\approx": "≈",
    "\\infty": "∞",
    "\\pi": "π",
    "\\alpha": "α",
    "\\beta": "β",
    "\\gamma": "γ",
    "\\delta": "δ",
    "\\theta": "θ",
    "\\lambda": "λ",
    "\\mu": "μ",
    "\\sigma": "σ",
    "\\phi": "φ",
    "\\omega": "ω",
    "\\sum": "∑",
    "\\int": "∫",
    "\\left": "",
    "\\right": "",
}


def _protect(pattern: re.Pattern[str], value: str, store: list[str], token: str) -> str:
    def repl(match: re.Match[str]) -> str:
        index = len(store)
        store.append(match.group(0))
        return token.format(index)

    return pattern.sub(repl, value)


def _protect_math_inline(value: str, store: list[str], token: str) -> str:
    def repl(match: re.Match[str]) -> str:
        inner = match.group(1)
        # Avoid mangling currency like $100 or prose like "$5 and $10".
        if not inner or inner.startswith(" ") or inner.endswith(" "):
            return match.group(0)
        if re.fullmatch(r"\d[\d,]*(?:\.\d+)?", inner):
            return match.group(0)
        if "  " in inner:
            return match.group(0)
        if " " in inner and not _MATHISH_RE.search(inner):
            return match.group(0)
        if not _MATHISH_RE.search(inner) and not re.fullmatch(r"[A-Za-z]", inner):
            return match.group(0)
        index = len(store)
        store.append(match.group(0))
        return token.format(index)

    return _MATH_INLINE_RE.sub(repl, value)


def _restore(value: str, store: list[str], token: str, transform) -> str:
    for index, raw in enumerate(store):
        value = value.replace(token.format(index), transform(raw))
    return value


def _sanitize_text(value: str) -> str:
    # Repair control chars that commonly corrupt LaTeX commands (e.g. \frac -> form-feed+rac).
    value = (
        value.replace("\x08", "\\b")
        .replace("\x0c", "\\f")
        .replace("\t", "    ")
    )
    return _CONTROL_RE.sub("", value)


def _render_math_html(expr: str) -> str:
    """Render simple LaTeX-like math as natural HTML (VPAM-style where possible)."""
    s = expr.strip()
    s = re.sub(r"[ \t]+", " ", s)
    tags: list[str] = []

    def tag(html_tag: str) -> str:
        i = len(tags)
        tags.append(html_tag)
        return f"@@MATHTAG{i}@@"

    def render_frac(num_src: str, den_src: str) -> str:
        num = _render_math_inner(num_src, tags)
        den = _render_math_inner(den_src, tags)
        return tag(
            '<span style="display:inline-table; vertical-align:middle; text-align:center; margin:0 2px;">'
            '<span style="display:table-row;">'
            f'<span style="display:table-cell; padding:0 4px 1px 4px; '
            f'border-bottom:1px solid currentColor; line-height:1.1;">{num}</span></span>'
            '<span style="display:table-row;">'
            f'<span style="display:table-cell; padding:1px 4px 0 4px; '
            f'line-height:1.1;">{den}</span></span></span>'
        )

    out = _render_math_inner(s, tags, render_frac=render_frac)
    for _ in range(12):
        changed = False
        for i, html_tag in enumerate(tags):
            token = f"@@MATHTAG{i}@@"
            if token in out:
                out = out.replace(token, html_tag)
                changed = True
        if not changed:
            break
    for i, html_tag in enumerate(tags):
        out = out.replace(f"@@MATHTAG{i}@@", html_tag)
    return out


def _render_math_inner(
    expr: str,
    tags: list[str],
    *,
    render_frac=None,
) -> str:
    s = expr
    if render_frac is not None:
        for _ in range(8):
            new = _FRAC_RE.sub(lambda m: render_frac(m.group(1), m.group(2)), s)
            if new == s:
                break
            s = new

    def tag(html_tag: str) -> str:
        i = len(tags)
        tags.append(html_tag)
        return f"@@MATHTAG{i}@@"

    s = _SQRT_RE.sub(lambda m: tag(f"√({_render_math_inner(m.group(1), tags)})"), s)

    def _apply_scripts(text: str) -> str:
        _sentinel = re.compile(r"@@MATHTAG\d+@@")
        parts = _sentinel.split(text)
        sentinels = _sentinel.findall(text)
        for i, part in enumerate(parts):
            part = _SUP_BRACE_RE.sub(
                lambda m: tag(f"<sup>{_render_math_inner(m.group(1), tags)}</sup>"), part
            )
            part = _SUB_BRACE_RE.sub(
                lambda m: tag(f"<sub>{_render_math_inner(m.group(1), tags)}</sub>"), part
            )
            part = _SUP_RE.sub(lambda m: tag(f"<sup>{html.escape(m.group(1))}</sup>"), part)
            part = _SUB_RE.sub(lambda m: tag(f"<sub>{html.escape(m.group(1))}</sub>"), part)
            parts[i] = part
        return "".join(
            p + (sentinels[i] if i < len(sentinels) else "") for i, p in enumerate(parts)
        )

    s = _apply_scripts(s)

    for key, value in _MATH_SYMBOLS.items():
        s = s.replace(key, value)
    s = s.replace("\\,", " ").replace("\\;", " ").replace("\\!", "").replace("\\ ", " ")
    s = s.replace("{", "").replace("}", "")

    # Escape plain text, but preserve our sentinels.
    parts = re.split(r"(@@MATHTAG\d+@@)", s)
    for idx, part in enumerate(parts):
        if part.startswith("@@MATHTAG") and part.endswith("@@"):
            continue
        parts[idx] = html.escape(part)
    return "".join(parts)


def markdown_to_html(text: str, theme: Theme) -> str:
    try:
        if not text:
            return ""
        value = str(text).replace("\r\n", "\n").replace("\r", "\n")
        value = _sanitize_text(value)
        # Qt's rich-text layout can choke on pathological size/structure.
        if len(value) > 120_000:
            value = value[:120_000] + "\n\n[message truncated for display]"

        # Protect code/math/links before escaping so raw content survives intact.
        blocks: list[str] = []
        inline: list[str] = []
        math_blocks: list[str] = []
        math_inline: list[str] = []
        links: list[str] = []
        value = _protect(_FENCE_RE, value, blocks, _CODE_TOKEN)
        value = _protect(_INLINE_CODE_RE, value, inline, _INLINE_TOKEN)
        value = _protect(_MATH_BLOCK_RE, value, math_blocks, "@@BLAZE_MATH_BLOCK_{}@@")
        value = _protect_math_inline(value, math_inline, "@@BLAZE_MATH_INLINE_{}@@")
        value = _protect(_LINK_RE, value, links, "@@BLAZE_LINK_{}@@")

        escaped = html.escape(value)
        return _markdown_to_html_impl(escaped, theme, blocks, inline, math_blocks, math_inline, links)
    except Exception:
        safe = html.escape(_sanitize_text(str(text or "")))[:120_000]
        return safe.replace("\n", "<br/>")


def _markdown_to_html_impl(
    escaped: str,
    theme: Theme,
    blocks: list[str],
    inline: list[str],
    math_blocks: list[str],
    math_inline: list[str],
    links: list[str],
) -> str:
    escaped = _BARE_LINK_RE.sub(
        lambda m: (
            f'<a href="{html.escape(m.group(1), quote=True)}" '
            f'style="color:{theme.accent}; text-decoration:none;">'
            f"{html.escape(m.group(1))}</a>"
        ),
        escaped,
    )
    escaped = _BOLD_RE.sub(r"<b>\1</b>", escaped)
    escaped = _ITALIC_RE.sub(r"<i>\1</i>", escaped)
    escaped = _UNDERLINE_RE.sub(r"<u>\1</u>", escaped)

    # Natural-looking powers in plain text (outside protected code/math): x^2, 10^{6}.
    # Keep sentinel tokens intact.
    _sentinel = re.compile(r"@@[A-Z_]+\d+@@")
    parts = _sentinel.split(escaped)
    sentinels = _sentinel.findall(escaped)
    for i, part in enumerate(parts):
        part = re.sub(
            r"\^\{([^{}\n]{1,20})\}",
            lambda m: f"<sup>{m.group(1)}</sup>",
            part,
        )
        part = re.sub(
            r"([A-Za-z0-9)\]])\^([n0-9+\-][A-Za-z0-9+\-]{0,4})\b",
            lambda m: f"{m.group(1)}<sup>{m.group(2)}</sup>",
            part,
        )
        parts[i] = part
    escaped = "".join(
        p + (sentinels[i] if i < len(sentinels) else "") for i, p in enumerate(parts)
    )

    lines = escaped.split("\n")
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            output.append("<br/>")
            continue
        if stripped.startswith("### "):
            output.append(
                f'<div style="font-size:14px; font-weight:600; margin:8px 0 3px 0;">'
                f"{stripped[4:]}</div>"
            )
            continue
        if stripped.startswith("## "):
            output.append(
                f'<div style="font-size:16px; font-weight:600; margin:8px 0 3px 0;">'
                f"{stripped[3:]}</div>"
            )
            continue
        if stripped.startswith("# "):
            output.append(
                f'<div style="font-size:18px; font-weight:600; margin:9px 0 4px 0;">'
                f"{stripped[2:]}</div>"
            )
            continue
        if stripped.startswith("&gt; "):
            output.append(
                f'<span style="color:{theme.muted};">▍ {stripped[5:]}</span><br/>'
            )
            continue
        bullet = _BULLET_RE.match(stripped)
        if bullet:
            output.append(
                f'<span style="color:{theme.accent};">•</span> {bullet.group(1)}<br/>'
            )
            continue
        output.append(stripped + "<br/>")
    rendered = "".join(output)

    def render_block(raw: str) -> str:
        match = _FENCE_RE.match(raw)
        if not match:
            return html.escape(raw).replace("\n", "<br/>")
        language = match.group(1).strip()
        code = html.escape(match.group(2).strip("\n"))
        label = (
            f'<div style="color:{theme.faint}; font-family:monospace; '
            f'font-size:9px; letter-spacing:1px; margin-bottom:4px;">'
            f"{html.escape(language.upper() or 'CODE')}</div>"
            if language
            else ""
        )
        return (
            f'<div style="background-color:{theme.surface_alt}; '
            f'border:1px solid {theme.border}; border-radius:7px; '
            f'padding:9px 11px; margin:6px 0;">'
            f'{label}<pre style="margin:0; font-family:monospace; '
            f'font-size:11px; white-space:pre-wrap;">{code}</pre></div>'
        )

    def render_inline(raw: str) -> str:
        match = _INLINE_CODE_RE.match(raw)
        code = html.escape(match.group(1) if match else raw.strip("`"))
        return (
            f'<code style="background-color:{theme.surface_alt}; '
            f'font-family:monospace; font-size:11px; '
            f'padding:1px 4px; border-radius:3px;">{code}</code>'
        )

    def render_link(raw: str) -> str:
        match = _LINK_RE.match(raw)
        if not match:
            return html.escape(raw)
        label = html.escape(match.group(1))
        url = html.escape(match.group(2), quote=True)
        return (
            f'<a href="{url}" style="color:{theme.accent}; '
            f'text-decoration:none;">{label}</a>'
        )

    def render_math_block(raw: str) -> str:
        match = _MATH_BLOCK_RE.match(raw)
        inner = _render_math_html(match.group(1).strip() if match else raw.strip("$"))
        return (
            f'<div style="background-color:{theme.surface_alt}; '
            f'border:1px solid {theme.border}; border-radius:7px; '
            f'padding:8px 11px; margin:6px 0; font-family:monospace; '
            f'font-size:13px; text-align:center;">{inner}</div>'
        )

    def render_math_inline(raw: str) -> str:
        match = _MATH_INLINE_RE.match(raw)
        inner = _render_math_html(match.group(1) if match else raw.strip("$"))
        return (
            f'<span style="background-color:{theme.surface_alt}; '
            f'border-radius:4px; padding:1px 5px; font-family:monospace; '
            f'font-size:12px;">{inner}</span>'
        )

    rendered = _restore(rendered, blocks, _CODE_TOKEN, render_block)
    rendered = _restore(rendered, inline, _INLINE_TOKEN, render_inline)
    rendered = _restore(rendered, math_blocks, "@@BLAZE_MATH_BLOCK_{}@@", render_math_block)
    rendered = _restore(rendered, math_inline, "@@BLAZE_MATH_INLINE_{}@@", render_math_inline)
    rendered = _restore(rendered, links, "@@BLAZE_LINK_{}@@", render_link)
    return rendered
