"""Dependency-free inline SVG charts for HTML reports."""

from __future__ import annotations

from html import escape

PASS, PARTIAL, FAIL, ERROR = "#2e8540", "#d8b13a", "#c9822b", "#a31726"
CHART_CSS = (
    "figure{margin:24px 0}figcaption{font-weight:650;margin-bottom:8px}"
    "svg{width:100%;height:auto;font:13px system-ui,sans-serif;fill:#17233b}"
)


def _label(text: str, limit: int = 44) -> str:
    return escape(text if len(text) <= limit else text[: limit - 1] + "\u2026")


def bar_chart(title: str, rows: list[tuple[str, float, str]]) -> str:
    """Horizontal 0-100% bars; rows are (label, percent, detail)."""
    bars = []
    for index, (label, percent, detail) in enumerate(rows):
        y = 10 + index * 34
        width = max(0.0, min(percent, 100.0)) * 4.2
        color = PASS if percent >= 80 else PARTIAL if percent >= 50 else FAIL if percent > 0 else ERROR
        bars.append(
            f'<text x="290" y="{y + 17}" text-anchor="end">{_label(label)}</text>'
            f'<rect x="300" y="{y}" width="420" height="24" fill="#e7ebf2" rx="4"/>'
            f'<rect x="300" y="{y}" width="{width:.1f}" height="24" fill="{color}" rx="4"/>'
            f'<text x="730" y="{y + 17}">{percent:.0f}% {escape(detail)}</text>'
        )
    return (
        f"<figure><figcaption>{escape(title)}</figcaption>"
        f'<svg viewBox="0 0 960 {34 * len(rows) + 20}" role="img" aria-label="{escape(title)}">'
        f"{''.join(bars)}</svg></figure>"
    )


def stacked_chart(
    title: str,
    rows: list[tuple[str, tuple[int, ...]]],
    categories: list[tuple[str, str]],
) -> str:
    """Horizontal stacked bars of counts; categories are (name, color)."""
    parts = []
    for index, (label, counts) in enumerate(rows):
        y, x, total = 10 + index * 34, 300.0, sum(counts) or 1
        parts.append(f'<text x="290" y="{y + 17}" text-anchor="end">{_label(label)}</text>')
        for count, (_, color) in zip(counts, categories):
            width = 420 * count / total
            if width:
                parts.append(f'<rect x="{x:.1f}" y="{y}" width="{width:.1f}" height="24" fill="{color}"/>')
            x += width
        parts.append(
            f'<text x="730" y="{y + 17}">'
            + escape(" / ".join(str(count) for count in counts))
            + "</text>"
        )
    legend_y = 20 + 34 * len(rows)
    for index, (name, color) in enumerate(categories):
        parts.append(
            f'<rect x="{300 + index * 160}" y="{legend_y}" width="14" height="14" fill="{color}"/>'
            f'<text x="{320 + index * 160}" y="{legend_y + 12}">{escape(name)}</text>'
        )
    return (
        f"<figure><figcaption>{escape(title)}</figcaption>"
        f'<svg viewBox="0 0 960 {legend_y + 24}" role="img" aria-label="{escape(title)}">'
        f"{''.join(parts)}</svg></figure>"
    )


def line_chart(title: str, points: list[tuple[str, float]], y_label: str) -> str:
    """Single-series line chart; points are (x label, value)."""
    if not points:
        return ""
    top = max(max(value for _, value in points), 1)
    step = 820 / max(len(points) - 1, 1)
    coords = [(100 + index * step, 210 - 180 * value / top) for index, (_, value) in enumerate(points)]
    labels = "".join(
        f'<text x="{x:.0f}" y="232" text-anchor="middle">{escape(label)}</text>'
        for index, ((label, _), (x, _)) in enumerate(zip(points, coords))
        if index % max(len(points) // 10, 1) == 0
    )
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    return (
        f"<figure><figcaption>{escape(title)}</figcaption>"
        f'<svg viewBox="0 0 960 250" role="img" aria-label="{escape(title)}">'
        f'<line x1="100" y1="210" x2="920" y2="210" stroke="#99a"/>'
        f'<line x1="100" y1="30" x2="100" y2="210" stroke="#99a"/>'
        f'<text x="90" y="34" text-anchor="end">{top:g}</text>'
        f'<text x="90" y="214" text-anchor="end">0</text>'
        f'<text x="20" y="120">{escape(y_label)}</text>'
        f'<polyline points="{path}" fill="none" stroke="#164c93" stroke-width="2.5"/>'
        f"{labels}</svg></figure>"
    )
