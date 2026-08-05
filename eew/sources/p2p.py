"""P2P地震情報 (wss://api.p2pquake.net/v2/ws) 受信。

  556: 緊急地震速報(警報)  -> Aggregator へ
  554: EEW発表検知 (詳細なし・最速シグナル) -> 表示のみ
  551: 地震情報 (確定値)   -> トースト通知
  552: 津波予報            -> トースト通知 + 警報音
"""
from __future__ import annotations

import asyncio
import json
from typing import Callable

import websockets

from .. import display, notifier, region
from ..models import EEWEvent, intensity_rank, now_jst, p2p_scale_label, parse_jst

URL = "wss://api.p2pquake.net/v2/ws"
SOURCE = "p2p"

_TSUNAMI_GRADE = {"MajorWarning": "大津波警報", "Warning": "津波警報", "Watch": "津波注意報"}


def _parse_556(data: dict) -> EEWEvent | None:
    eq = data.get("earthquake") or {}
    hypo = eq.get("hypocenter") or {}
    origin = parse_jst(str(eq.get("originTime", "")), "%Y/%m/%d %H:%M:%S")
    if origin is None:
        return None

    issue = data.get("issue") or {}
    try:
        serial = int(issue.get("serial") or 0)
    except (ValueError, TypeError):
        serial = 0

    areas = []
    for a in data.get("areas") or []:
        name = a.get("name") if isinstance(a, dict) else None
        if name:
            areas.append(name)

    def _num(v):
        try:
            f = float(v)
            return f if f > -200 else None  # P2P は欠測を -1 等で返すことがある
        except (ValueError, TypeError):
            return None

    depth = _num(hypo.get("depth"))
    return EEWEvent(
        source=SOURCE,
        # Wolfx / kmoni の EEW イベントID (発生時刻 yyyyMMddHHmmss) に揃える
        event_id=origin.strftime("%Y%m%d%H%M%S"),
        serial=serial,
        hypocenter=str(hypo.get("name") or ""),
        magnitude=_num(hypo.get("magnitude")),
        depth_km=int(depth) if depth is not None and depth >= 0 else None,
        max_intensity="",  # 556 は最大震度そのものは持たない (警報地域のみ)
        latitude=_num(hypo.get("latitude")),
        longitude=_num(hypo.get("longitude")),
        origin_time=origin,
        announced_time=parse_jst(str(issue.get("time", "")), "%Y/%m/%d %H:%M:%S"),
        is_warn=True,  # 556 は警報のみ
        is_cancel=bool(data.get("cancelled")),
        is_training=bool(data.get("test")),
        warn_areas=areas,
    )


class P2PSource:
    def __init__(self, on_eew: Callable[[EEWEvent], None], cfg: dict,
                 publish: Callable[[dict], None] | None = None):
        self.on_eew = on_eew
        self.cfg = cfg
        self.publish = publish or (lambda payload: None)
        self.ncfg = cfg.get("notify", {})
        self.forecast_min_rank = intensity_rank(
            self.ncfg.get("forecast_min_intensity", "3"))
        self._last_554 = None  # 554 のレート制限

    def _handle_554(self) -> None:
        now = now_jst()
        if self._last_554 and (now - self._last_554).total_seconds() < 30:
            return
        self._last_554 = now
        display.log(SOURCE, f"{display.YELLOW}緊急地震速報の発表を検知 "
                            f"(詳細待ち){display.RESET}")

    def _handle_551(self, data: dict) -> None:
        eq = data.get("earthquake") or {}
        hypo = eq.get("hypocenter") or {}
        scale = eq.get("maxScale")
        label = p2p_scale_label(scale if isinstance(scale, int) and scale > 0 else None)
        name = hypo.get("name") or "震源不明"
        mag = hypo.get("magnitude")
        mag_s = f" M{mag:.1f}" if isinstance(mag, (int, float)) and mag >= 0 else ""
        depth = hypo.get("depth")
        depth_s = f" 深さ{int(depth)}km" if isinstance(depth, (int, float)) and depth >= 0 else ""
        tsunami = eq.get("domesticTsunami")
        tsunami_s = {"None": "津波の心配なし", "Unknown": "津波不明",
                     "Checking": "津波調査中", "NonEffective": "若干の海面変動",
                     "Watch": "津波注意報", "Warning": "津波予報(警報)"}.get(tsunami, "")

        msg = f"地震情報: {name}{mag_s}{depth_s} 最大震度{label} {tsunami_s}"
        display.log(SOURCE, msg, display.BOLD)

        rank = intensity_rank(label)
        if rank >= self.forecast_min_rank:
            notifier.toast("地震情報", f"{name}{mag_s} / 最大震度{label} / {tsunami_s}",
                           urgent=False, enabled=self.ncfg.get("toast_enabled", True))
            notifier.play_sound("info", self.ncfg.get("sound_enabled", True))

    def _handle_552(self, data: dict) -> None:
        if data.get("cancelled"):
            display.log(SOURCE, "津波予報: 解除されました", display.GREEN)
            notifier.toast("津波予報 解除", "津波予報は解除されました",
                           enabled=self.ncfg.get("toast_enabled", True))
            self.publish({"type": "tsunami", "cancelled": True, "grades": {},
                          "home_grade": None})
            return
        grades = {}
        for a in data.get("areas") or []:
            g = _TSUNAMI_GRADE.get(a.get("grade"), a.get("grade") or "")
            grades.setdefault(g, []).append(a.get("name") or "")

        # 自分事判定: 自宅都道府県が対象の予報区に含まれるか (最上位の種別を採用)
        home_pref = (self.cfg.get("home") or {}).get("_pref") or ""
        home_grade = None
        if home_pref:
            for g in ("大津波警報", "津波警報", "津波注意報"):
                if any(region.matches_area(home_pref, n)
                       for n in grades.get(g, [])):
                    home_grade = g
                    break

        summary = " / ".join(f"{g}: {'、'.join(names[:6])}" for g, names in grades.items())
        display.log(SOURCE, f"{display.WHITE_ON_RED}【津波予報】{display.RESET} {summary}")

        # 行動指示は段階で変える (注意報に「高台へ避難」は過剰)
        def _action(grade):
            return ("海岸や川から離れてください" if grade == "津波注意報"
                    else "ただちに海岸・川から離れて高台へ避難してください")

        if home_grade:
            display.log_system(
                f"{display.WHITE_ON_RED}【{home_pref}の沿岸に{home_grade}】"
                f"{_action(home_grade)}{display.RESET}")
            notifier.toast(f"{home_grade} - あなたの地域が対象です",
                           _action(home_grade),
                           urgent=home_grade != "津波注意報",
                           enabled=self.ncfg.get("toast_enabled", True))
        else:
            notifier.toast("津波予報 (他地域)", summary or "津波予報が発表されました",
                           urgent=False, enabled=self.ncfg.get("toast_enabled", True))

        # 音も段階で変える: 警報級=津波音 / 注意報のみ=チャイム音
        top = next((g for g in ("大津波警報", "津波警報", "津波注意報") if g in grades),
                   "津波注意報")
        effective = home_grade or top
        kind = "tsunami" if effective in ("大津波警報", "津波警報") else "tsunami_watch"
        notifier.play_sound(kind, self.ncfg.get("sound_enabled", True))
        self.publish({"type": "tsunami", "cancelled": False, "grades": grades,
                      "home_grade": home_grade})

    def handle(self, data: dict) -> None:
        code = data.get("code")
        if code == 556:
            ev = _parse_556(data)
            if ev:
                self.on_eew(ev)
        elif code == 554:
            self._handle_554()
        elif code == 551:
            self._handle_551(data)
        elif code == 552:
            self._handle_552(data)

    async def run(self) -> None:
        backoff = 1
        while True:
            try:
                async with websockets.connect(URL, ping_interval=30, ping_timeout=15) as ws:
                    display.log_system("P2P地震情報: 接続しました", display.GREEN)
                    backoff = 1
                    async for raw in ws:
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(data, dict):
                            self.handle(data)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                display.log_system(
                    f"P2P地震情報: 切断 ({type(e).__name__}: {e}) {backoff}秒後に再接続",
                    display.YELLOW)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
