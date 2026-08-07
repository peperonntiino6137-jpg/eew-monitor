"""実際の地震の永続ログ (logs/events/*.jsonl) の閲覧。

使い方:
  python -m eew.history                # 直近20件を時系列で一覧
  python -m eew.history --limit 50     # 件数を変える
  python -m eew.history --days 7       # 直近7日分だけ
  python -m eew.history --event <ID>   # 1イベントの全報を詳細表示
  python -m eew.history --json         # 機械可読 (JSON) で出力
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from .models import JST, intensity_rank, rank_label

ROOT = Path(__file__).resolve().parent.parent
EVENTS_DIR = ROOT / "logs" / "events"


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def load_records(days: int | None = None) -> list[dict]:
    """全月別ファイルを読み、recorded_at 昇順のレコードリストを返す。"""
    records: list[dict] = []
    if not EVENTS_DIR.is_dir():
        return records
    cutoff = None
    if days is not None:
        cutoff = datetime.now(JST) - timedelta(days=days)
    for path in sorted(EVENTS_DIR.glob("*.jsonl")):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = _parse_dt(rec.get("recorded_at"))
                    if ts is None:
                        continue
                    if cutoff is not None and ts < cutoff:
                        continue
                    rec["_ts"] = ts
                    records.append(rec)
        except OSError:
            continue
    records.sort(key=lambda r: r["_ts"])
    return records


def summarize_eew(records: list[dict]) -> list[dict]:
    """EEW レコードを event_id ごとに 1 イベントへ集約する。"""
    events: dict[str, dict] = {}
    for r in records:
        if r.get("type") != "eew":
            continue
        ev = events.setdefault(r["event_id"], {
            "event_id": r["event_id"],
            "time": None, "hypocenter": "", "magnitude": None, "depth_km": None,
            "max_intensity_rank": -1, "max_intensity": "",
            "is_warn": False, "is_cancel": False, "is_final": False,
            "max_serial": 0, "sources": set(), "reasons": [],
            "in_warn_area": False, "home": None, "records": [],
            "first_recorded": r["_ts"], "last_recorded": r["_ts"],
        })
        ev["records"].append(r)
        ev["last_recorded"] = r["_ts"]
        ot = _parse_dt(r.get("origin_time"))
        if ot is not None:
            ev["time"] = ot
        if r.get("hypocenter"):
            ev["hypocenter"] = r["hypocenter"]
        # 仮定震源 (PLUM) の M・深さはダミー値なので採用しない
        if not r.get("is_assumption"):
            if r.get("magnitude") is not None:
                ev["magnitude"] = r["magnitude"]
            if r.get("depth_km") is not None:
                ev["depth_km"] = r["depth_km"]
        rank = intensity_rank(r.get("max_intensity"))
        if rank > ev["max_intensity_rank"]:
            ev["max_intensity_rank"] = rank
            ev["max_intensity"] = r.get("max_intensity") or ""
        ev["is_warn"] = ev["is_warn"] or bool(r.get("is_warn"))
        ev["is_cancel"] = ev["is_cancel"] or bool(r.get("is_cancel"))
        ev["is_final"] = ev["is_final"] or bool(r.get("is_final"))
        ev["max_serial"] = max(ev["max_serial"], int(r.get("serial") or 0))
        ev["sources"].add(r.get("source") or "?")
        if r.get("reason"):
            ev["reasons"].append(r["reason"])
        ev["in_warn_area"] = ev["in_warn_area"] or bool(r.get("in_warn_area"))
        if r.get("home") is not None:
            ev["home"] = r["home"]
    out = list(events.values())
    for ev in out:
        if ev["time"] is None:
            ev["time"] = ev["first_recorded"]
    out.sort(key=lambda e: e["time"])
    return out


def _fmt_eew_line(ev: dict) -> str:
    t = ev["time"].strftime("%m/%d %H:%M:%S")
    kind = "取消" if ev["is_cancel"] else ("警報" if ev["is_warn"] else "予報")
    parts = [f"{t} [EEW/{kind}]", ev["hypocenter"] or "震源不明"]
    if ev["magnitude"] is not None:
        parts.append(f"M{ev['magnitude']:.1f}")
    if ev["depth_km"] is not None:
        parts.append(f"深さ{ev['depth_km']}km")
    if ev["max_intensity"]:
        parts.append(f"最大震度{ev['max_intensity']}")
    parts.append(f"第{ev['max_serial']}報まで({'/'.join(sorted(ev['sources']))})")
    if ev["home"]:
        h = ev["home"]
        parts.append(f"自宅:予想{h.get('intensity', '?')}"
                     f" {h.get('epicentral_km', '?')}km")
    if ev["in_warn_area"]:
        parts.append("自宅=警報地域")
    if ev["reasons"]:
        parts.append(f"通知:{'/'.join(dict.fromkeys(ev['reasons']))}")
    else:
        parts.append("通知なし")
    parts.append(f"ID:{ev['event_id']}")
    return "  ".join(parts)


def _fmt_other_line(r: dict) -> str:
    t = r["_ts"].strftime("%m/%d %H:%M:%S")
    if r["type"] == "info":
        parts = [f"{t} [確定]", r.get("hypocenter") or "震源不明"]
        if r.get("magnitude") is not None:
            parts.append(f"M{r['magnitude']:.1f}")
        if r.get("depth_km") is not None:
            parts.append(f"深さ{int(r['depth_km'])}km")
        if r.get("max_intensity"):
            parts.append(f"最大震度{r['max_intensity']}")
        if r.get("tsunami"):
            parts.append(r["tsunami"])
        if r.get("points"):
            parts.append(f"(観測{len(r['points'])}地点)")
        return "  ".join(parts)
    if r["type"] == "tsunami":
        if r.get("cancelled"):
            return f"{t} [津波]  解除"
        summary = " / ".join(f"{g}:{len(names)}区" for g, names in
                             (r.get("grades") or {}).items())
        home = f"  自宅地域={r['home_grade']}" if r.get("home_grade") else ""
        return f"{t} [津波]  {summary}{home}"
    if r["type"] == "ground_motion":
        return (f"{t} [揺れ検知]  {r.get('region', '?')}付近"
                f" 震度{r.get('intensity', '?')}相当"
                f" ({r.get('point_count', '?')}観測点)")
    return f"{t} [{r['type']}]"


def show_list(records: list[dict], limit: int) -> None:
    events = summarize_eew(records)
    items: list[tuple[datetime, str]] = [
        (ev["time"], _fmt_eew_line(ev)) for ev in events]
    items += [(r["_ts"], _fmt_other_line(r)) for r in records
              if r.get("type") != "eew"]
    items.sort(key=lambda x: x[0])
    if not items:
        print("記録された地震イベントはまだありません"
              f" ({EVENTS_DIR} が空か未作成)")
        return
    shown = items[-limit:] if limit > 0 else items
    if len(shown) < len(items):
        print(f"... 他 {len(items) - len(shown)} 件 (--limit で表示件数を増やせます)")
    for _, line in shown:
        print(line)


def show_event(records: list[dict], event_id: str) -> None:
    recs = [r for r in records
            if r.get("type") == "eew" and r.get("event_id") == event_id]
    if not recs:
        print(f"イベント {event_id} の記録が見つかりません")
        sys.exit(1)
    ev = summarize_eew(recs)[0]
    print(_fmt_eew_line(ev))
    print("-" * 60)
    for r in recs:
        t = r["_ts"].strftime("%H:%M:%S.%f")[:-3]
        flags = "".join([
            "警" if r.get("is_warn") else "予",
            "終" if r.get("is_final") else "",
            "取" if r.get("is_cancel") else "",
            "仮" if r.get("is_assumption") else "",
        ])
        parts = [t, f"[{r.get('source', '?'):5s}]", f"第{r.get('serial')}報", flags]
        if r.get("magnitude") is not None:
            parts.append(f"M{r['magnitude']:.1f}")
        if r.get("depth_km") is not None:
            parts.append(f"深さ{r['depth_km']}km")
        if r.get("max_intensity"):
            parts.append(f"震度{r['max_intensity']}")
        if r.get("home"):
            h = r["home"]
            parts.append(f"自宅:予想{h.get('intensity', '?')}")
        if r.get("latency_ms") is not None:
            parts.append(f"遅延{r['latency_ms']}ms")
        if r.get("reason"):
            parts.append(f"通知:{r['reason']}")
        print("  ".join(parts))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="実際の地震の永続ログを閲覧する")
    parser.add_argument("--days", type=int, default=None,
                        help="直近N日分だけ表示 (省略時は全期間)")
    parser.add_argument("--limit", type=int, default=20,
                        help="一覧の表示件数 (default: 20、0で全件)")
    parser.add_argument("--event", metavar="ID",
                        help="指定イベントIDの全報を詳細表示")
    parser.add_argument("--json", action="store_true",
                        help="JSON で出力 (集約済みイベント+その他レコード)")
    args = parser.parse_args()

    records = load_records(args.days)

    if args.json:
        events = summarize_eew(records)
        for ev in events:
            ev["sources"] = sorted(ev["sources"])
            ev["time"] = ev["time"].isoformat()
            ev["first_recorded"] = ev["first_recorded"].isoformat()
            ev["last_recorded"] = ev["last_recorded"].isoformat()
            for r in ev["records"]:
                r.pop("_ts", None)
        others = [{k: v for k, v in r.items() if k != "_ts"}
                  for r in records if r.get("type") != "eew"]
        print(json.dumps({"events": events, "others": others},
                         ensure_ascii=False, indent=2))
        return

    if args.event:
        show_event(records, args.event)
    else:
        show_list(records, args.limit)


if __name__ == "__main__":
    main()
