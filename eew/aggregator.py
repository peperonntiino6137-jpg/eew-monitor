"""複数ソースからの EEW を重複排除し、通知判断を行う中枢。

方針: 最速で届いたソースが「新規イベント」を発火し、
      以降の同一イベントは続報 (serial 増加) のみ表示・更新する。
追加: 自宅地点の予想震度・S波到達カウントダウン、地図へのプッシュ。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from . import display, eventlog, notifier, recorder, region
from .estimate import HomeEstimate, estimate_home, haversine_km
from .models import (EEWEvent, GroundMotionEvent, intensity_rank, now_jst,
                     rank_label)

# ソース間でイベントIDが数秒ズレるときの同一イベント判定 (P2P は originTime 由来のため)
MERGE_TIME_SEC = 12.0
MERGE_DIST_KM = 150.0
PRUNE_AFTER_SEC = 1800  # 終息した古いイベントの保持期限
RANK_INT3 = intensity_rank("3")  # 専用音 (全国/自宅の震度3以上) のしきい値
INFO_MATCH_SEC = 90.0        # 確定情報と EEW イベントの同一判定 (発生時刻差)
INFO_RECORD_SEC = 45.0       # EEW を伴わない確定情報の収録時間 (静的表示のため短め)


@dataclass
class _EventState:
    max_serial: int = 0
    max_intensity_rank: int = -1
    warned: bool = False           # 警報として通知済みか
    finalized: bool = False
    cancelled: bool = False
    sounded_home3: bool = False      # 「震度3以上到達予定」音を鳴らした
    sounded_national3: bool = False  # 「全国震度3以上」音を鳴らした
    first_source: str = ""
    sources: set[str] = field(default_factory=set)
    latest: EEWEvent | None = None
    estimate: HomeEstimate | None = None
    last_update: object = None     # datetime
    recording: recorder.Recording | None = None  # 地図画面の収録 (震度3以上)


class Aggregator:
    def __init__(self, cfg: dict, publish: Callable[[dict], None] | None = None):
        self.cfg = cfg
        self.publish = publish or (lambda payload: None)
        self.events: dict[str, _EventState] = {}
        self.stale_seconds = cfg.get("stale_seconds", 180)

        ncfg = cfg.get("notify", {})
        self.forecast_min_rank = intensity_rank(
            ncfg.get("forecast_min_intensity", "3"))
        self.use_home_intensity = bool(ncfg.get("use_home_intensity", True))

        home = cfg.get("home", {}) or {}
        lat, lon = home.get("latitude"), home.get("longitude")
        self.home: tuple[float, float] | None = None
        self.avs30 = float(home.get("avs30") or 300)
        self.home_name = home.get("name") or "自宅"
        self.home_pref = home.get("_pref") or ""
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            self.home = (float(lat), float(lon))

        self._countdowns: dict[str, asyncio.Task] = {}
        self._aliases: dict[str, str] = {}  # 別ソースのズレたID -> 正規ID
        # EEW を伴わない確定情報の収録 (発生時刻キーで続報 551 と重複開始しない)
        self._info_recordings: dict[str, recorder.Recording] = {}

    def set_home(self, lat: float, lon: float, name: str | None = None) -> None:
        """自宅地点を実行中に更新 (地図クリックからの反映用)。"""
        self.home = (float(lat), float(lon))
        if name:
            self.home_name = name

    # ------------------------------------------------------------------ utils

    def eew_active(self, within_seconds: float = 180) -> bool:
        """直近に活動中の EEW があるか (揺れ検知の抑制用)。"""
        now = now_jst()
        for st in self.events.values():
            if st.cancelled:
                continue
            if st.last_update and (now - st.last_update).total_seconds() < within_seconds:
                return True
        return False

    def _is_stale(self, ev: EEWEvent) -> bool:
        base = ev.announced_time or ev.origin_time
        if base is None:
            return False
        return (now_jst() - base).total_seconds() > self.stale_seconds

    def _resolve_event_id(self, ev: EEWEvent) -> str:
        """ソース間の ID ズレを吸収して正規イベントIDを返す。

        Wolfx/kmoni は JMA の EEW 識別番号で一致するが、P2P は originTime から
        合成するため数秒ズレる (続報で originTime が改定されるとさらに変わる)。
        発生時刻±MERGE_TIME_SEC かつ震央 MERGE_DIST_KM 以内なら同一地震とみなす。
        """
        if ev.event_id in self.events:
            return ev.event_id
        if ev.event_id in self._aliases:
            return self._aliases[ev.event_id]
        if ev.origin_time is not None:
            for key, st in self.events.items():
                if st.cancelled:
                    continue  # 取消済みへ統合すると再発表が握り潰される
                le = st.latest
                if le is None or le.origin_time is None:
                    continue
                dt = abs((ev.origin_time - le.origin_time).total_seconds())
                if dt > MERGE_TIME_SEC:
                    continue
                if (ev.latitude is not None and le.latitude is not None
                        and haversine_km(ev.latitude, ev.longitude,
                                         le.latitude, le.longitude) > MERGE_DIST_KM):
                    continue
                self._aliases[ev.event_id] = key
                display.log(ev.source,
                            f"同一地震と判定し {key} に統合 (ID: {ev.event_id})",
                            display.DIM)
                return key
        return ev.event_id

    def _prune(self) -> None:
        """終息した古いイベントとカウントダウンタスクを間引く (常駐時のメモリ対策)。"""
        now = now_jst()
        dead = [k for k, st in self.events.items()
                if st.last_update and (now - st.last_update).total_seconds() > PRUNE_AFTER_SEC]
        for k in dead:
            st = self.events.pop(k, None)
            if st is not None and st.recording is not None:
                st.recording.stop()
            task = self._countdowns.pop(k, None)
            if task is not None and not task.done():
                task.cancel()
        if dead:
            self._aliases = {a: k for a, k in self._aliases.items() if k in self.events}
        for k in [k for k, t in self._countdowns.items() if t.done()]:
            self._countdowns.pop(k, None)

    def _publish_eew(self, ev: EEWEvent, st: _EventState) -> None:
        est = st.estimate
        payload = {
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
            "is_warn": ev.is_warn,
            "is_final": ev.is_final,
            "is_cancel": ev.is_cancel or st.cancelled,
            "is_assumption": ev.is_assumption,
            "warn_areas": ev.warn_areas,
            "home": None,
        }
        payload["server_now"] = now_jst().isoformat()  # クライアント時計補正用
        # 自分事判定: 警報対象地域に自宅都道府県が含まれるか
        in_warn_area = None
        if ev.is_warn and self.home_pref and ev.warn_areas:
            in_warn_area = any(region.matches_area(self.home_pref, a)
                               for a in ev.warn_areas)
        payload["home_pref"] = self.home_pref or None
        payload["in_warn_area"] = in_warn_area
        if est is not None:
            payload["home"] = {
                "intensity_label": est.intensity_label,
                "rank": est.intensity_rank,
                "instrumental": round(est.instrumental, 1),
                "epicentral_km": round(est.epicentral_km),
                "s_arrival": est.s_arrival.isoformat() if est.s_arrival else None,
                "p_arrival": est.p_arrival.isoformat() if est.p_arrival else None,
            }
        self.publish(payload)

    # ------------------------------------------------------------- countdown

    def _ensure_countdown(self, event_id: str) -> None:
        task = self._countdowns.get(event_id)
        if task is not None and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # テスト等、イベントループ外
        self._countdowns[event_id] = loop.create_task(
            self._countdown_loop(event_id), name=f"countdown-{event_id}")

    async def _countdown_loop(self, event_id: str) -> None:
        """S波到達カウントダウン。毎報の再計算結果 (st.estimate) を参照し続ける。"""
        last_shown: int | None = None
        arrived_shown = False
        while True:
            await asyncio.sleep(1)
            st = self.events.get(event_id)
            if st is None or st.cancelled:
                return
            est = st.estimate
            if est is None or est.s_arrival is None:
                return
            remaining = est.s_remaining(now_jst())
            if remaining is None:
                return

            # S波到達までは収録を延命する (続報が止まっても揺れの到達は映す)
            if st.recording is not None and remaining > -5:
                st.recording.touch()

            # 地図へは毎秒プッシュ
            self.publish({
                "type": "countdown",
                "event_id": event_id,
                "s_remaining": round(remaining, 1),
                "intensity_label": est.intensity_label,
                "server_now": now_jst().isoformat(),
            })

            if remaining <= -15:
                if est.intensity_rank >= 1:
                    display.log_system(f"[{self.home_name}] カウントダウン終了",
                                       display.DIM)
                return

            # 予想震度0 (揺れを感じない) はコンソールに流さない。
            # 連発時のノイズ対策で、地図へのプッシュは上で継続している
            if est.intensity_rank < 1:
                continue

            # コンソール表示の間引き: >30s は10秒毎 / >5s は5秒毎 / それ以下は毎秒
            r = int(remaining)
            # 複数地震の同時進行時に区別できるよう震源名を付ける
            hypo = st.latest.hypocenter if st.latest else ""
            if remaining <= 0:
                if not arrived_shown:
                    arrived_shown = True
                    display.log_system(
                        f"{display.WHITE_ON_RED}[{self.home_name}] {hypo}の地震の"
                        f"S波到達 (予想震度 {est.intensity_label}){display.RESET}")
                continue
            show = (r <= 5) or (r <= 30 and r % 5 == 0) or (r % 10 == 0)
            if show and r != last_shown:
                last_shown = r
                color = display.RED if r <= 10 else display.YELLOW
                display.log_system(
                    f"{color}[{self.home_name}] {hypo}: S波到達まで 約{r}秒 "
                    f"/ 予想震度 {est.intensity_label}(暫定){display.RESET}")

    # ------------------------------------------------------------------- EEW

    def handle_eew(self, ev: EEWEvent) -> None:
        if ev.is_training:
            display.log(ev.source, "訓練報を受信 (無視)", display.DIM)
            return

        # 接続直後に流れてくる過去イベントを除外
        if self._is_stale(ev):
            display.log(ev.source, f"過去のEEWを受信 (無視): {ev.hypocenter} 第{ev.serial}報",
                        display.DIM)
            return

        self._prune()
        # ソース間のIDズレを正規IDへ吸収 (以降 event_id は正規IDとして扱う)
        ev.event_id = self._resolve_event_id(ev)

        st = self.events.get(ev.event_id)
        is_new = st is None
        if is_new:
            st = _EventState(first_source=ev.source)
            self.events[ev.event_id] = st
        st.sources.add(ev.source)
        st.last_update = now_jst()

        # ---- 取消 ----
        if ev.is_cancel:
            if not st.cancelled:
                st.cancelled = True
                display.show_eew(ev, is_new=False)
                eventlog.log_eew(ev, st.estimate, "cancel", None)
                if st.recording is not None:
                    st.recording.cancelled = True
                    st.recording.stop()
                notifier.notify_eew(ev, self.cfg, "cancel")
                self._publish_eew(ev, st)
            return

        # 取消済みイベントへの遅延続報 (順序逆転した final 等) は無視する
        if st.cancelled:
            display.log(ev.source, f"取消済みイベントの続報を無視: 第{ev.serial}報",
                        display.DIM)
            return

        # ---- 重複判定: 既に見た報数以下なら別ソースの遅れ着信 ----
        if not is_new and ev.serial <= st.max_serial:
            became_warn = ev.is_warn and not st.warned
            rank_up = ev.intensity_rank > st.max_intensity_rank
            if not became_warn and not rank_up:
                return  # 完全な重複。表示もしない
        # 古い報 (別ソースの遅れ着信) で最新状態を巻き戻さない
        if ev.serial >= st.max_serial:
            st.latest = ev
        st.max_serial = max(st.max_serial, ev.serial)

        # ---- 自宅地点の推定 (毎報再計算して補正) ----
        home_est = None
        if self.home is not None:
            home_est = estimate_home(ev, self.home[0], self.home[1], self.avs30)
            if home_est is not None:
                st.estimate = home_est

        # ---- 表示 ----
        display.show_eew(ev, is_new=is_new)
        in_warn_area = None
        if ev.is_warn and self.home_pref and ev.warn_areas:
            in_warn_area = any(region.matches_area(self.home_pref, a)
                               for a in ev.warn_areas)
            if in_warn_area:
                display.log_system(
                    f"{display.WHITE_ON_RED}【{self.home_pref}は警報対象地域です】"
                    f"{display.RESET}")
        if home_est is not None:
            remaining = home_est.s_remaining(now_jst())
            if remaining is not None and remaining > 0:
                arr = f" S波到達まで約{int(remaining)}秒"
            elif remaining is not None:
                arr = " S波到達済み"
            else:
                arr = ""
            display.log_system(
                f"[{self.home_name}] 予想震度 {home_est.intensity_label}(暫定) "
                f"震央距離{home_est.epicentral_km:.0f}km{arr} ※±数秒の目安",
                display.CYAN)
            self._ensure_countdown(ev.event_id)

        # ---- 地図へプッシュ ----
        self._publish_eew(ev, st)

        # ---- 通知判断 ----
        reason = None
        if is_new:
            reason = "new"
        elif ev.is_warn and not st.warned:
            reason = "upgrade"      # 予報 -> 警報 格上げ
        elif ev.intensity_rank >= 5 and ev.intensity_rank > st.max_intensity_rank:
            reason = "upgrade"      # 震度5弱以上への上方修正 (震度不明からの確定を含む)
        elif ev.is_final and not st.finalized:
            reason = "final"

        st.warned = st.warned or ev.is_warn
        st.max_intensity_rank = max(st.max_intensity_rank, ev.intensity_rank)
        st.finalized = st.finalized or ev.is_final

        # ---- 地図画面の収録 (最大震度3以上、続報で条件を満たした時点から) ----
        if st.recording is None:
            st.recording = recorder.maybe_start(ev.event_id, st.max_intensity_rank)
        if st.recording is not None:
            st.recording.touch()
            st.recording.update_meta(
                hypocenter=ev.hypocenter or None,
                magnitude=None if ev.is_assumption else ev.magnitude,
                intensity_label=(rank_label(st.max_intensity_rank)
                                 if st.max_intensity_rank >= 0 else None))

        # ---- 震度3以上の専用音の条件 ----
        est_now = home_est or st.estimate
        home3 = est_now is not None and est_now.intensity_rank >= RANK_INT3
        national3 = ev.intensity_rank >= RANK_INT3

        # ---- 永続ログ (後から python -m eew.history で振り返る用) ----
        eventlog.log_eew(ev, est_now, reason, in_warn_area)

        if reason is None:
            # 通知しない続報でも、続報の上方修正で条件を初めて満たしたら音だけ鳴らす
            self._play_int3_sound(ev, st, home3, national3)
            return

        # 予報の通知フィルタ (警報は常に通知する)
        if not ev.is_warn and reason in ("new", "final"):
            # この報で推定できなくても直近の有効推定 (st.estimate) で判定を継続する
            est_filter = home_est or st.estimate
            if self.use_home_intensity and est_filter is not None:
                # 自宅予想震度ベース: 遠方の地震で鳴らさない
                if est_filter.intensity_rank < self.forecast_min_rank:
                    # 通知対象外の遠方でも「全国震度3以上」なら音だけ鳴らす
                    self._play_int3_sound(ev, st, home3, national3)
                    return
            elif ev.intensity_rank < self.forecast_min_rank:
                return

        # 予報の通知音を条件に応じて差し替える (警報音はより上位としてそのまま)
        sound_kind = None
        if not ev.is_warn:
            if home3:
                sound_kind = "home_int3"
            elif national3:
                sound_kind = "national_int3"
        if reason in ("new", "upgrade"):
            # notify_eew が音を鳴らす場面: 専用音の重複再生を防ぐフラグだけ立てる
            # (警報時は警報音が鳴るが、上位互換として鳴った扱いにする)
            if home3:
                st.sounded_home3 = st.sounded_national3 = True
            elif national3:
                st.sounded_national3 = True
        else:
            # final はトーストのみで音を鳴らさないため、未達ならここで専用音を補う
            self._play_int3_sound(ev, st, home3, national3)

        notifier.notify_eew(ev, self.cfg, reason, home_est, in_warn_area, sound_kind)

    def _play_int3_sound(self, ev: EEWEvent, st: _EventState,
                         home3: bool, national3: bool) -> None:
        """通知を出さない場面用: 震度3以上の条件を初めて満たしたら音だけ鳴らす。

        自宅到達予定 (home_int3) が全国 (national_int3) を兼ねるため、
        鳴るのはイベントごとに高々一度ずつ。
        """
        sound_on = self.cfg.get("notify", {}).get("sound_enabled", True)
        if home3 and not st.sounded_home3:
            st.sounded_home3 = st.sounded_national3 = True
            display.log_system(
                f"[{self.home_name}] 震度3以上が到達予定 ({ev.hypocenter})")
            notifier.play_sound("home_int3", sound_on)
        elif national3 and not home3 and not st.sounded_national3:
            st.sounded_national3 = True
            display.log_system(
                f"全国で震度3以上 ({ev.hypocenter} 最大震度{ev.max_intensity}) 音のみ",
                display.DIM)
            notifier.play_sound("national_int3", sound_on)

    # ----------------------------------------------------------- quake info

    def handle_quake_info(self, info: dict) -> None:
        """P2P 551 確定情報。EEW が出なかった地震も地図表示・収録の対象にする。

        info: p2p が組み立てた dict (hypocenter/magnitude/depth_km/max_intensity/
              tsunami/latitude/longitude/origin_time[iso])
        """
        # ---- 地図へプッシュ (EEW の有無に関わらず確定値を表示する) ----
        self.publish({"type": "quake_info", **info,
                      "server_now": now_jst().isoformat()})

        rank = intensity_rank(info.get("max_intensity"))
        key = info.get("origin_time") or ""

        # 同じ地震の続報 551 (震度速報 -> 震源情報 -> 詳細) では収録を増やさず
        # メタデータ (震源名・M・震度) だけ良い値に更新する
        rec = self._info_recordings.get(key)
        if rec is not None:
            rec.update_meta(hypocenter=info.get("hypocenter"),
                            magnitude=info.get("magnitude"),
                            intensity_label=info.get("max_intensity"))
            return

        # 対応する EEW イベントが既にあるか (発生時刻の近接で判定)
        ot = None
        if key:
            try:
                ot = datetime.fromisoformat(key)
            except ValueError:
                pass
        if ot is not None:
            for st in self.events.values():
                le = st.latest
                if le is None or le.origin_time is None:
                    continue
                if abs((ot - le.origin_time).total_seconds()) <= INFO_MATCH_SEC:
                    if st.recording is not None:
                        # EEW 側で収録済み/収録中。確定値の方が正確なので名前に反映
                        st.recording.update_meta(
                            hypocenter=info.get("hypocenter"),
                            magnitude=info.get("magnitude"),
                            intensity_label=info.get("max_intensity"))
                        return
                    break  # EEW はあったが収録条件未達 -> 確定値で判定し直す

        rec = recorder.maybe_start(f"info-{key or now_jst().strftime('%H%M%S')}",
                                   rank, duration=INFO_RECORD_SEC)
        if rec is not None:
            rec.update_meta(hypocenter=info.get("hypocenter"),
                            magnitude=info.get("magnitude"),
                            intensity_label=info.get("max_intensity"))
            if key:
                self._info_recordings[key] = rec
                if len(self._info_recordings) > 10:  # 常駐時の伸び対策
                    self._info_recordings.pop(next(iter(self._info_recordings)))

    # --------------------------------------------------------- ground motion

    def handle_ground_motion(self, gm: GroundMotionEvent) -> None:
        """強震モニタ画像解析による揺れ検知。EEW 発報中は抑制。"""
        if self.eew_active():
            return
        display.log("kmoni",
                    f"{display.YELLOW}揺れ検知(強震モニタ) {gm.region}付近 "
                    f"震度{gm.max_realtime_intensity:.1f}相当 "
                    f"({gm.point_count}観測点){display.RESET}")
        eventlog.log_ground_motion(gm)
        ncfg = self.cfg.get("notify", {})
        notifier.toast("揺れ検知 (強震モニタ)",
                       f"{gm.region}付近で震度{gm.max_realtime_intensity:.1f}相当の揺れを検知",
                       urgent=False, enabled=ncfg.get("toast_enabled", True))
        notifier.play_sound("info", ncfg.get("sound_enabled", True))
        self.publish({
            "type": "ground_motion",
            "region": gm.region,
            "intensity": round(gm.max_realtime_intensity, 1),
            "point_count": gm.point_count,
            "latitude": gm.latitude,
            "longitude": gm.longitude,
            "detected_at": gm.detected_at.isoformat(),
        })
