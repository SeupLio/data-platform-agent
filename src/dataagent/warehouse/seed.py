"""造一个 B 站风格的离线数仓（sqlite）。

为什么自己造：
  · 岗位是「数据平台」Agent，没有真实数仓权限也能把链路跑通；
  · 数据必须**可复现** —— 固定随机种子，任何人 clone 后跑出的数字与 README 一致；
  · 异常是**故意注入并登记在案**的（`meta_anomaly` 表），这样「自动分析 Agent 有没有归因对」
    才有客观答案，而不是靠人看着像。
"""

from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

START_DATE = date(2026, 3, 1)
END_DATE = date(2026, 9, 24)
SEED = 20260926

CATEGORIES = ("动画", "游戏", "知识", "生活", "影视", "音乐", "科技", "美食")
PLATFORMS = ("ios", "android", "web", "pc")

UP_COUNT = 100

# 各分区的量级与内容特征（基础播放、平均播放时长、完播倾向）
CATEGORY_PROFILE = {
    "动画": {"play": 5_200_000, "dur": 62, "finish": 0.42},
    "游戏": {"play": 6_800_000, "dur": 71, "finish": 0.36},
    "知识": {"play": 2_900_000, "dur": 96, "finish": 0.31},
    "生活": {"play": 3_400_000, "dur": 48, "finish": 0.39},
    "影视": {"play": 4_100_000, "dur": 105, "finish": 0.28},
    "音乐": {"play": 2_200_000, "dur": 55, "finish": 0.44},
    "科技": {"play": 1_900_000, "dur": 88, "finish": 0.33},
    "美食": {"play": 2_600_000, "dur": 41, "finish": 0.40},
}

PLATFORM_SHARE = {"ios": 0.34, "android": 0.41, "web": 0.17, "pc": 0.08}
PLATFORM_DAU = {"ios": 3_100_000, "android": 3_900_000, "web": 2_200_000, "pc": 1_050_000}

# ── 故意注入的异常（分析 Agent 的归因题标准答案） ────────────────────────────
ANOMALIES = (
    # 游戏分区在 2026-09-10 播放量断崖（-45%）
    {"date": "2026-09-10", "dim": "category", "value": "游戏", "metric": "play_cnt", "factor": 0.55},
    # PC 端 DAU 在 2026-09-16 突增（+38%）
    {"date": "2026-09-16", "dim": "platform", "value": "pc", "metric": "dau", "factor": 1.38},
)

DDL = """
CREATE TABLE IF NOT EXISTS dws_video_daily (
    date TEXT NOT NULL,
    category TEXT NOT NULL,
    platform TEXT NOT NULL,
    upload_cnt INTEGER, audit_pass_cnt INTEGER,
    play_cnt INTEGER, watch_uv INTEGER, finish_cnt INTEGER, valid_play_cnt INTEGER,
    duration_sec INTEGER,
    like_cnt INTEGER, coin_cnt INTEGER, fav_cnt INTEGER, share_cnt INTEGER,
    comment_cnt INTEGER, danmu_cnt INTEGER, triple_cnt INTEGER
);
CREATE TABLE IF NOT EXISTS dws_user_daily (
    date TEXT NOT NULL,
    platform TEXT NOT NULL,
    dau INTEGER, new_user_cnt INTEGER, retain_1d_cnt INTEGER, retain_7d_cnt INTEGER,
    usage_sec INTEGER,
    pay_user_cnt INTEGER, pay_amount REAL, vip_open_cnt INTEGER,
    ad_income REAL, ad_impression INTEGER, ad_click INTEGER
);
CREATE TABLE IF NOT EXISTS dws_up_daily (
    date TEXT NOT NULL,
    up_id TEXT NOT NULL,
    category TEXT NOT NULL,
    publish_cnt INTEGER, play_cnt INTEGER, follower_add_cnt INTEGER, income REAL
);
CREATE TABLE IF NOT EXISTS meta_anomaly (
    date TEXT, dim TEXT, value TEXT, metric TEXT, factor REAL
);
CREATE TABLE IF NOT EXISTS meta_seed (
    seed INTEGER, start_date TEXT, end_date TEXT, up_count INTEGER
);
"""


@dataclass
class SeedSummary:
    db_path: str
    start_date: str
    end_date: str
    days: int
    video_rows: int
    user_rows: int
    up_rows: int
    anomalies: int


def _seasonality(d: date) -> float:
    """周末更高 + 缓慢增长的季节性。"""
    weekend = 1.18 if d.weekday() >= 5 else 1.0
    trend = 1.0 + (d - START_DATE).days * 0.00045
    return weekend * trend


def _anomaly_factor(d: date, dim: str, value: str, metric: str) -> float:
    ds = d.isoformat()
    for a in ANOMALIES:
        if a["date"] == ds and a["dim"] == dim and a["value"] == value and a["metric"] == metric:
            return a["factor"]
    return 1.0


def generate(db_path: str | Path, seed: int = SEED) -> SeedSummary:
    rng = random.Random(seed)
    path = Path(db_path)
    if path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(DDL)
        days = (END_DATE - START_DATE).days + 1
        dates = [START_DATE + timedelta(days=i) for i in range(days)]

        video_rows: list[tuple] = []
        for d in dates:
            for cat in CATEGORIES:
                prof = CATEGORY_PROFILE[cat]
                for plat in PLATFORMS:
                    base = prof["play"] * PLATFORM_SHARE[plat] * _seasonality(d)
                    play = int(base * rng.uniform(0.94, 1.06))
                    play = int(play * _anomaly_factor(d, "category", cat, "play_cnt"))
                    watch_uv = int(play * rng.uniform(0.40, 0.45))
                    duration = int(play * prof["dur"] * rng.uniform(0.92, 1.08))
                    finish = int(play * prof["finish"] * rng.uniform(0.94, 1.06))
                    valid = int(play * rng.uniform(0.56, 0.68))
                    like = int(play * rng.uniform(0.030, 0.058))
                    video_rows.append(
                        (
                            d.isoformat(),
                            cat,
                            plat,
                            int(rng.uniform(900, 1500)),  # upload_cnt
                            int(rng.uniform(820, 1420)),  # audit_pass_cnt
                            play,
                            watch_uv,
                            finish,
                            valid,
                            duration,
                            like,
                            int(like * rng.uniform(0.24, 0.32)),  # coin
                            int(like * rng.uniform(0.44, 0.56)),  # fav
                            int(like * rng.uniform(0.09, 0.15)),  # share
                            int(play * rng.uniform(0.006, 0.011)),  # comment
                            int(play * rng.uniform(0.035, 0.062)),  # danmu
                            int(like * rng.uniform(0.15, 0.21)),  # triple
                        )
                    )
        conn.executemany(
            "INSERT INTO dws_video_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", video_rows
        )

        user_rows: list[tuple] = []
        for d in dates:
            for plat in PLATFORMS:
                dau = int(PLATFORM_DAU[plat] * _seasonality(d) * rng.uniform(0.96, 1.04))
                dau = int(dau * _anomaly_factor(d, "platform", plat, "dau"))
                new_user = int(dau * rng.uniform(0.018, 0.026))
                r1 = int(new_user * rng.uniform(0.33, 0.39))
                r7 = int(new_user * rng.uniform(0.16, 0.21))
                usage = int(dau * rng.uniform(1500, 2100))
                pay = int(dau * rng.uniform(0.010, 0.014))
                user_rows.append(
                    (
                        d.isoformat(),
                        plat,
                        dau,
                        new_user,
                        r1,
                        r7,
                        usage,
                        pay,
                        round(pay * rng.uniform(72, 96), 2),
                        int(dau * rng.uniform(0.0016, 0.0026)),
                        round(dau * rng.uniform(0.038, 0.055), 2),
                        int(dau * rng.uniform(2.5, 4.0)),
                        int(dau * rng.uniform(0.06, 0.11)),
                    )
                )
        conn.executemany(
            "INSERT INTO dws_user_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", user_rows
        )

        ups = [(f"UP{100000 + i}", CATEGORIES[i % len(CATEGORIES)]) for i in range(UP_COUNT)]
        up_rows: list[tuple] = []
        for d in dates:
            for up_id, cat in ups:
                if rng.random() > 0.38:
                    continue
                publish = int(rng.uniform(1, 4))
                play = int(rng.uniform(8_000, 260_000))
                up_rows.append(
                    (
                        d.isoformat(),
                        up_id,
                        cat,
                        publish,
                        play,
                        int(play * rng.uniform(0.002, 0.012)),
                        round(play * rng.uniform(0.004, 0.011), 2),
                    )
                )
        conn.executemany("INSERT INTO dws_up_daily VALUES (?,?,?,?,?,?,?)", up_rows)

        conn.executemany(
            "INSERT INTO meta_anomaly VALUES (?,?,?,?,?)",
            [(a["date"], a["dim"], a["value"], a["metric"], a["factor"]) for a in ANOMALIES],
        )
        conn.execute(
            "INSERT INTO meta_seed VALUES (?,?,?,?)",
            (seed, START_DATE.isoformat(), END_DATE.isoformat(), UP_COUNT),
        )
        conn.commit()
    finally:
        conn.close()

    return SeedSummary(
        db_path=str(path),
        start_date=START_DATE.isoformat(),
        end_date=END_DATE.isoformat(),
        days=days,
        video_rows=len(video_rows),
        user_rows=len(user_rows),
        up_rows=len(up_rows),
        anomalies=len(ANOMALIES),
    )


DEFAULT_DB = Path(__file__).parent / "data" / "warehouse.db"
