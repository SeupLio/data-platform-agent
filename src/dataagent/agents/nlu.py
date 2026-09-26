"""自然语言 → 结构化查询意图（时间 / 维度 / 筛选）。

刻意做成**确定性规则**而不是让大模型自由发挥：
时间区间和维度筛选错了，数字就会错得毫无痕迹。规则能被测、能被复盘，
大模型在这一层只应该做「兜底改写成规则能认的形式」，不应该直接产出日期。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from ..knowledge.schema import Metric

PLATFORM_ALIASES = {
    "ios": "ios",
    "iphone": "ios",
    "ios端": "ios",
    "苹果": "ios",
    "苹果端": "ios",
    "android": "android",
    "安卓": "android",
    "安卓端": "android",
    "android端": "android",
    "web": "web",
    "网页": "web",
    "网页端": "web",
    "web端": "web",
    "h5": "web",
    "pc": "pc",
    "电脑": "pc",
    "电脑端": "pc",
    "pc端": "pc",
    "桌面端": "pc",
}

DIMENSION_HINTS = {
    "category": ("分区", "各分区", "品类", "按分区", "分分区", "按品类", "各品类", "分区维度"),
    "platform": ("端", "各端", "按端", "分端", "终端", "客户端", "按平台", "各平台"),
}


@dataclass(frozen=True)
class TimeRange:
    start: str
    end: str
    label: str

    def as_dict(self) -> dict[str, str]:
        return {"start": self.start, "end": self.end, "label": self.label}


@dataclass(frozen=True)
class ParsedQuery:
    raw: str
    time_range: TimeRange
    dimensions: tuple[str, ...]
    filters: tuple[tuple[str, str], ...]


_DAYS_RE = re.compile(r"(?:最近|近|过去|过去)\s*(\d+)\s*天")
_RANGE_RE = re.compile(
    r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})\s*(?:日|号)?\s*(?:到|至|-|~|—)\s*"
    r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})"
)
_SINGLE_RE = re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})\s*(?:日|号)?")
_MONTH_RE = re.compile(r"(?:(\d{4})\s*年\s*)?(\d{1,2})\s*月(?!\s*\d)")


def _d(y: int, m: int, d: int) -> date:
    return date(y, m, d)


def parse_time(text: str, today: date | str) -> TimeRange:
    """把时间说法解析成闭区间。

    `today` 由调用方给（数仓里的最大日期），**不读系统时间**，保证可复现。
    """
    t = today if isinstance(today, date) else date.fromisoformat(today)
    q = text or ""

    m = _RANGE_RE.search(q)
    if m:
        s = _d(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        e = _d(int(m.group(4)), int(m.group(5)), int(m.group(6)))
        return TimeRange(s.isoformat(), e.isoformat(), f"{s.isoformat()} 至 {e.isoformat()}")

    if "今年以来" in q or "年初至今" in q:
        s = _d(t.year, 1, 1)
        return TimeRange(s.isoformat(), t.isoformat(), "今年以来")

    if "前天" in q:
        d = t - timedelta(days=2)
        return TimeRange(d.isoformat(), d.isoformat(), "前天")
    if "昨天" in q or "昨日" in q:
        d = t - timedelta(days=1)
        return TimeRange(d.isoformat(), d.isoformat(), "昨天")
    if "今天" in q or "今日" in q:
        return TimeRange(t.isoformat(), t.isoformat(), "今天")

    m = _DAYS_RE.search(q)
    if m:
        n = int(m.group(1))
        s = t - timedelta(days=n - 1)
        return TimeRange(s.isoformat(), t.isoformat(), f"最近 {n} 天")

    if "上周" in q:
        monday = t - timedelta(days=t.weekday())
        s = monday - timedelta(days=7)
        e = s + timedelta(days=6)
        return TimeRange(s.isoformat(), e.isoformat(), "上周")
    if "本周" in q or "这周" in q:
        s = t - timedelta(days=t.weekday())
        return TimeRange(s.isoformat(), t.isoformat(), "本周")
    if "上月" in q or "上个月" in q:
        first = _d(t.year, t.month, 1)
        last_prev = first - timedelta(days=1)
        s = _d(last_prev.year, last_prev.month, 1)
        return TimeRange(s.isoformat(), last_prev.isoformat(), "上月")
    if "本月" in q or "这个月" in q:
        s = _d(t.year, t.month, 1)
        return TimeRange(s.isoformat(), t.isoformat(), "本月")

    m = _MONTH_RE.search(q)
    if m:
        year = int(m.group(1)) if m.group(1) else t.year
        month = int(m.group(2))
        s = _d(year, month, 1)
        nxt = _d(year + (month == 12), (month % 12) + 1, 1)
        e = nxt - timedelta(days=1)
        return TimeRange(s.isoformat(), e.isoformat(), f"{year} 年 {month} 月")

    m = _SINGLE_RE.search(q)
    if m:
        d = _d(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return TimeRange(d.isoformat(), d.isoformat(), d.isoformat())

    # 默认：最近 7 天（产品上也是看板默认值）
    s = t - timedelta(days=6)
    return TimeRange(s.isoformat(), t.isoformat(), "最近 7 天")


def parse_filters(text: str, categories: tuple[str, ...] = ()) -> tuple[tuple[str, str], ...]:
    filters: list[tuple[str, str]] = []
    q = (text or "").lower()
    for alias, value in PLATFORM_ALIASES.items():
        if alias in q:
            filters.append(("platform", value))
            break
    for cat in categories:
        if cat and cat in (text or ""):
            filters.append(("category", cat))
            break
    return tuple(filters)


def parse_dimensions(text: str, metric: Metric | None = None) -> tuple[str, ...]:
    dims: list[str] = []
    for dim, hints in DIMENSION_HINTS.items():
        if any(h in (text or "") for h in hints):
            dims.append(dim)
    allowed = metric.dimensions if metric else None
    if allowed is not None:
        dims = [d for d in dims if d in allowed]
    return tuple(dict.fromkeys(dims))


def parse(text: str, today: date | str, metric: Metric | None = None,
          categories: tuple[str, ...] = ()) -> ParsedQuery:
    return ParsedQuery(
        raw=text,
        time_range=parse_time(text, today),
        dimensions=parse_dimensions(text, metric),
        filters=parse_filters(text, categories),
    )
