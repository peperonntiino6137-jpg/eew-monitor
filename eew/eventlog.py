"""実際の地震イベントの永続ログ (JSONL)。

後から `python -m eew.history` で振り返るための構造化ログ。
logs/events/YYYY-MM.jsonl に 1 行 1 レコードで追記する。

コンソール/ヘッドレスのログ (logs/eew.log) はローテーションで消えるが、
こちらは月別ファイルで残り続ける。地震は稀なのでサイズは問題にならない。

main.py が enable() を呼んだときだけ書き込む。test_alert / demo は
enable() しないため、テスト・デモの疑似イベントは記録されない。
"""
from __future__ import annotations

import json
from pathlib import Path

from .estimate import HomeEstimate
from .models import EEWEvent, GroundMotionEvent, now_jst

_dir: Path | None = None


def enable(directory: str | Path) -> None:
    global _dir
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    _dir = d


def _append(record: dict) -> None:
    if _dir is None:
        return
    now = now_jst()
    record["recorded_at"] = now.isoformat()
    path = _dir / f"{now:%Y-%m}.jsonl"
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 記録失敗で受信処理を止めない


def log_eew(ev: EEWEvent, home_est: HomeEstimate | None,
            reason: str | None, in_warn_area: bool | None) -> None:
    """EEW 1 報 (重複排除を通過したもの)。reason は通知判断の結果。"""
    record = {
        "type": "eew",
        "event_id": ev.event_id,
        "source": ev.source,
        "serial": ev.serial,
        "hypocenter": ev.hypocenter,
        "magnitude": ev.magnitude,
        "depth_km": ev.depth_km,
        "max_intensity": ev.max_intensity,
        "latitude": ev.latitude,
        "longitude": ev.longitude,
        "origin_time": ev.origin_time.isoformat() if ev.origin_time else None,
        "announced_time": (ev.announced_time.isoformat()
                           if ev.announced_time else None),
        "is_warn": ev.is_warn,
        "is_final": ev.is_final,
        "is_cancel": ev.is_cancel,
        "is_assumption": ev.is_assumption,
        "warn_areas": ev.warn_areas,
        "latency_ms": ev.latency_ms(),
        "reason": reason,          # "new"/"upgrade"/"final"/"cancel"/None(通知なし)
        "in_warn_area": in_warn_area,
        "home": None,
    }
    if home_est is not None:
        record["home"] = {
            "intensity": home_est.intensity_label,
            "rank": home_est.intensity_rank,
            "instrumental": round(home_est.instrumental, 2),
            "epicentral_km": round(home_est.epicentral_km, 1),
            "s_arrival": (home_est.s_arrival.isoformat()
                          if home_est.s_arrival else None),
        }
    _append(record)


def log_info(record: dict) -> None:
    """P2P 551 地震情報 (確定値)。観測点別震度も残す (後の検証用)。"""
    _append({"type": "info", **record})


def log_tsunami(grades: dict, cancelled: bool, home_grade: str | None) -> None:
    _append({"type": "tsunami", "cancelled": cancelled,
             "grades": grades, "home_grade": home_grade})


def log_ground_motion(gm: GroundMotionEvent) -> None:
    _append({
        "type": "ground_motion",
        "region": gm.region,
        "intensity": round(gm.max_realtime_intensity, 1),
        "point_count": gm.point_count,
        "latitude": gm.latitude,
        "longitude": gm.longitude,
    })
