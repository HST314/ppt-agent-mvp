from __future__ import annotations

import hashlib
import html
from dataclasses import dataclass
from typing import Mapping

from ..generation.contracts import DeckSpec, SlideSpec
from ..generation.errors import AssetResolutionError
from .assets import ResolvedAsset
from .theme import css_theme


RENDERER_VERSION = "1.0.0"


BASE_CSS = """*{box-sizing:border-box}html,body{margin:0;background:#111}body{font-family:var(--font-body);color:var(--text)}.deck{display:flex;flex-direction:column;align-items:center;gap:24px;padding:24px}.slide{position:relative;width:1280px;height:720px;overflow:hidden;background:var(--background);padding:64px 72px;display:grid;grid-template-rows:auto 1fr auto;gap:calc(var(--space)*2)}.slide-page-number{position:absolute;right:32px;bottom:24px;color:var(--muted-text);font-size:14px}.slide-title{margin:0;font:700 42px/1.15 var(--font-heading);letter-spacing:-.02em}.slide-content{min-height:0;display:grid;gap:calc(var(--space)*1.5);align-content:center}.layout-cover .slide-content,.layout-statement .slide-content,.layout-closing .slide-content{align-content:center}.layout-columns .slide-content{grid-template-columns:repeat(2,minmax(0,1fr))}.layout-metrics .slide-content{grid-template-columns:repeat(3,minmax(0,1fr))}.block{min-width:0}.block-heading{font:650 30px/1.2 var(--font-heading)}.block-paragraph,.block-bullets,.block-table{font-size:21px;line-height:1.45}.block-bullets{margin:0;padding-left:1.35em}.block-bullets li+li{margin-top:calc(var(--space)*.6)}.block-metric{border-radius:var(--radius);background:var(--surface);padding:calc(var(--space)*1.5)}.metric-value{font:700 38px/1 var(--font-heading);color:var(--primary)}.metric-label{margin-top:var(--space);font-size:17px;color:var(--muted-text)}.block-table{width:100%;border-collapse:collapse;background:var(--surface);border-radius:var(--radius);overflow:hidden}.block-table td{padding:calc(var(--space)*.65);border:1px solid color-mix(in srgb,var(--muted-text),transparent 70%)}.block-image{margin:0;min-height:0}.block-image img{display:block;width:100%;height:100%;max-height:420px;object-fit:contain}.block-image figcaption{font-size:14px;color:var(--muted-text)}.block-quote{margin:0;border-left:6px solid var(--accent);padding-left:calc(var(--space)*1.5);font:500 28px/1.35 var(--font-heading)}.quote-attribution{display:block;margin-top:var(--space);font:16px/1.3 var(--font-body);color:var(--muted-text)}.speaker-notes{display:none}"""
WRAP_CSS = ".slide-title,.block,.block-table td{overflow-wrap:anywhere}"
DENSITY_CSS = ".slide-density-compact{padding:48px 64px;gap:var(--space)}.slide-density-compact .slide-title{font-size:36px}.slide-density-compact .slide-content{gap:calc(var(--space)*.75)}.slide-density-compact .block-heading{font-size:26px}.slide-density-compact .block-paragraph,.slide-density-compact .block-bullets,.slide-density-compact .block-table{font-size:18px;line-height:1.3}.slide-density-compact .block-metric{padding:var(--space)}.slide-density-compact .metric-value{font-size:32px}.slide-density-compact .metric-label{font-size:16px;margin-top:calc(var(--space)*.5)}.slide-density-compact .block-table td{padding:calc(var(--space)*.4)}.slide-density-compact .block-quote{font-size:24px;line-height:1.25}"


@dataclass(frozen=True)
class RenderedDeck:
    html: str
    sha256: str
    renderer_version: str
    slide_hashes: dict[str, str]


class DeterministicRenderer:
    version = RENDERER_VERSION

    def render(self, deck: DeckSpec, assets: Mapping[str, ResolvedAsset] | None = None, *, language: str = "zh-CN") -> RenderedDeck:
        assets = assets or {}
        required = set(deck.shared_assets)
        if set(assets) != required:
            raise AssetResolutionError("renderer 资源映射与 DeckSpec 闭包不一致")
        slides = [self._render_slide(slide, index + 1, assets) for index, slide in enumerate(deck.slides)]
        title = html.escape(deck.slides[0].title, quote=True)
        source = "\n".join((
            "<!doctype html>",
            f'<html lang="{html.escape(language, quote=True)}">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width,initial-scale=1">',
            f'<meta name="renderer-version" content="{self.version}">',
            f"<title>{title}</title>",
            f"<style>{css_theme(deck.theme_tokens)}{BASE_CSS}{WRAP_CSS}{DENSITY_CSS}</style>",
            "</head>",
            "<body>",
            f'<main class="deck" data-renderer-version="{self.version}">',
            *slides,
            "</main>",
            "</body>",
            "</html>",
            "",
        ))
        return RenderedDeck(
            html=source,
            sha256=hashlib.sha256(source.encode()).hexdigest(),
            renderer_version=self.version,
            slide_hashes={slide.slide_id: hashlib.sha256(fragment.encode()).hexdigest() for slide, fragment in zip(deck.slides, slides)},
        )

    def _render_slide(self, slide: SlideSpec, page_number: int, assets: Mapping[str, ResolvedAsset]) -> str:
        title_tag = "h1" if slide.layout_family == "cover" else "h2"
        blocks = [self._render_block(block.to_dict(), assets) for block in slide.content_blocks]
        density_class = " slide-density-compact" if len(blocks) >= 3 else ""
        notes = html.escape(slide.speaker_notes)
        return "\n".join((
            f'<section class="slide layout-{slide.layout_family}{density_class}" id="{slide.slide_id}" data-slide-id="{slide.slide_id}" data-layout-family="{slide.layout_family}" data-visual-role="{slide.role}" data-page-number="{page_number}">',
            f'<{title_tag} class="slide-title" data-element-id="title">{html.escape(slide.title)}</{title_tag}>',
            '<div class="slide-content" data-element-id="content">',
            *blocks,
            "</div>",
            f'<div class="slide-page-number" data-element-id="page-number">{page_number}</div>',
            f'<aside class="speaker-notes" hidden>{notes}</aside>',
            "</section>",
        ))

    @staticmethod
    def _render_block(block: dict, assets: Mapping[str, ResolvedAsset]) -> str:
        kind, block_id = block["type"], block["block_id"]
        prefix = f'class="block block-{kind}" data-element-id="{block_id}"'
        if kind == "heading":
            return f'<div {prefix}>{html.escape(block["text"])}</div>'
        if kind == "paragraph":
            return f'<p {prefix}>{html.escape(block["text"])}</p>'
        if kind == "bullets":
            items = "".join(f"<li>{html.escape(item)}</li>" for item in block["items"])
            return f'<ul {prefix}>{items}</ul>'
        if kind == "metric":
            return f'<div {prefix}><div class="metric-value">{html.escape(block["value"])}</div><div class="metric-label">{html.escape(block["label"])}</div></div>'
        if kind == "table":
            rows = "".join("<tr>" + "".join(f"<td>{html.escape(cell)}</td>" for cell in row) + "</tr>" for row in block["rows"])
            return f'<table {prefix}><tbody>{rows}</tbody></table>'
        if kind == "image":
            asset = assets.get(block["asset_ref"])
            if asset is None:
                raise AssetResolutionError("页面引用资源未解析")
            alt = html.escape(block["alt"], quote=True)
            return f'<figure {prefix}><img src="{asset.offline_path}" alt="{alt}"><figcaption>{alt}</figcaption></figure>'
        if kind == "quote":
            return f'<blockquote {prefix}>{html.escape(block["text"])}<cite class="quote-attribution">{html.escape(block["attribution"])}</cite></blockquote>'
        raise AssertionError(f"unhandled block type: {kind}")
