"""Windows トースト通知と警報音。

警報音は config.json の notify.sounds で音源ファイル (WAV/MP3) に差し替え可能。
未設定・ファイル欠損時は内蔵ビープにフォールバックする。
"""
from __future__ import annotations

import ctypes
import threading
from pathlib import Path

from . import display
from .models import EEWEvent, now_jst

try:
    import winsound
except ImportError:  # 非 Windows 環境
    winsound = None

try:
    from winotify import Notification, audio
except ImportError:
    Notification = None
    audio = None

APP_ID = "緊急地震速報モニタ"

ROOT = Path(__file__).resolve().parent.parent

# kind -> 音源ファイルパス (configure() が設定)
_sound_files: dict[str, Path] = {}
_mci_lock = threading.Lock()
_mci_counter = 0


def configure(cfg: dict) -> None:
    """config.json の notify.sounds を読み込む。相対パスはプロジェクト基準。"""
    global _sound_files
    _sound_files = {}
    sounds = (cfg.get("notify") or {}).get("sounds") or {}
    for kind, path in sounds.items():
        if not path:
            continue
        p = Path(path)
        if not p.is_absolute():
            p = ROOT / p
        if p.is_file():
            _sound_files[kind] = p
        else:
            display.log_system(f"音源ファイルが見つかりません ({kind}): {p}",
                               display.YELLOW)
    if _sound_files:
        names = ", ".join(f"{k}={v.name}" for k, v in _sound_files.items())
        display.log_system(f"カスタム音源: {names}", display.GREEN)


def _play_file(path: Path) -> None:
    """WAV/MP3 を Windows MCI で非同期再生する。"""
    global _mci_counter
    try:
        with _mci_lock:
            _mci_counter += 1
            alias = f"eewsnd{_mci_counter}"
        mci = ctypes.windll.winmm.mciSendStringW
        # 前々回分の後始末は close で自動 (エラーは無視)
        mci(f'open "{path}" alias {alias}', None, 0, None)
        mci(f"play {alias} from 0", None, 0, None)
        # 再生終了後に閉じる (最長60秒で強制クローズ)
        def _closer():
            mci(f"close {alias}", None, 0, None)
        threading.Timer(60, _closer).start()
    except Exception as e:
        display.log_system(f"音源再生失敗 ({type(e).__name__}): {path.name}",
                           display.YELLOW)


def _beep_pattern(pattern: list[tuple[int, int]], repeat: int) -> None:
    if winsound is None:
        return
    for _ in range(repeat):
        for freq, ms in pattern:
            try:
                winsound.Beep(freq, ms)
            except RuntimeError:
                return


def play_sound(kind: str, enabled: bool = True) -> None:
    """kind: 'warning' | 'forecast' | 'info' | 'tsunami'"""
    if not enabled:
        return
    # カスタム音源があればそれを再生 (WAV/MP3)
    custom = _sound_files.get(kind)
    if custom is not None:
        threading.Thread(target=_play_file, args=(custom,), daemon=True).start()
        return
    if winsound is None:
        return
    patterns = {
        # 警報: 高音を強く繰り返す
        "warning": ([(880, 180), (1175, 180), (880, 180), (1175, 180)], 3),
        # 予報: 2 音 × 2
        "forecast": ([(660, 150), (880, 200)], 2),
        # 津波: 低音の長い繰り返し
        "tsunami": ([(523, 400), (392, 400)], 3),
        # 確定情報など: 短い 1 音
        "info": ([(740, 180)], 1),
    }
    pattern, repeat = patterns.get(kind, patterns["info"])
    threading.Thread(target=_beep_pattern, args=(pattern, repeat), daemon=True).start()


def _do_toast(title: str, message: str, urgent: bool) -> None:
    if Notification is None:
        return
    try:
        toast = Notification(
            app_id=APP_ID,
            title=title,
            msg=message,
            duration="long" if urgent else "short",
        )
        if audio is not None:
            # 音は play_sound (カスタム音源) が担当。トースト自身は無音にして二重再生を防ぐ
            toast.set_audio(audio.Silent, loop=False)
        toast.show()
    except Exception as e:  # 通知失敗でアプリを止めない
        display.log_system(f"トースト通知失敗: {e}", display.YELLOW)


def toast(title: str, message: str, urgent: bool = False, enabled: bool = True) -> None:
    if not enabled:
        return
    threading.Thread(target=_do_toast, args=(title, message, urgent), daemon=True).start()


def notify_eew(ev: EEWEvent, cfg: dict, reason: str, home_est=None) -> None:
    """EEW をトースト+音で通知する。reason: 'new' | 'upgrade' | 'final' | 'cancel'

    home_est: estimate.HomeEstimate (あれば自宅予想震度・到達秒数を本文に含める)
    """
    ncfg = cfg.get("notify", {})
    sound_on = ncfg.get("sound_enabled", True)
    toast_on = ncfg.get("toast_enabled", True)

    kind = "警報" if ev.is_warn else "予報"

    if reason == "cancel":
        toast(f"緊急地震速報({kind}) 取消", f"{ev.hypocenter} の速報は取り消されました",
              urgent=False, enabled=toast_on)
        play_sound("info", sound_on)
        return

    body_parts = []
    if ev.hypocenter:
        body_parts.append(f"震源: {ev.hypocenter}")
    if ev.magnitude is not None:
        body_parts.append(f"M{ev.magnitude:.1f}")
    if ev.max_intensity:
        body_parts.append(f"最大震度 {ev.max_intensity}")
    if home_est is not None:
        body_parts.append(f"自宅予想震度 {home_est.intensity_label}(暫定)")
        remaining = home_est.s_remaining(now_jst())
        if remaining is not None and remaining > 0:
            body_parts.append(f"S波まで約{int(remaining)}秒")
    body_parts.append(f"第{ev.serial}報" + ("(最終)" if ev.is_final else ""))
    body = " / ".join(body_parts)

    title_prefix = {"new": "", "upgrade": "【更新】", "final": "【最終報】"}.get(reason, "")
    toast(f"{title_prefix}緊急地震速報({kind})", body, urgent=ev.is_warn, enabled=toast_on)

    if reason in ("new", "upgrade"):
        play_sound("warning" if ev.is_warn else "forecast", sound_on)
