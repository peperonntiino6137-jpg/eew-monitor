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


def _mci(cmd: str) -> tuple[int, str]:
    """mciSendStringW 1 回。(エラーコード, 応答文字列) を返す。0 = 成功。"""
    buf = ctypes.create_unicode_buffer(256)
    err = ctypes.windll.winmm.mciSendStringW(cmd, buf, 255, None)
    return err, buf.value


def _mci_error_text(err: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    ctypes.windll.winmm.mciGetErrorStringW(err, buf, 255)
    return buf.value or f"MCIエラー {err}"


def _play_file(path: Path) -> bool:
    """WAV/MP3 を Windows MCI で再生する。開始できたら True。

    失敗 (コーデック非対応・ファイル破損等) は呼び出し側でビープに退避させる。
    """
    global _mci_counter
    with _mci_lock:
        _mci_counter += 1
        alias = f"eewsnd{_mci_counter}"
    try:
        err, _ = _mci(f'open "{path}" alias {alias}')
        if err:
            display.log_system(
                f"音源再生失敗 ({_mci_error_text(err)}): {path.name}、内蔵ビープで代替",
                display.YELLOW)
            return False
        # 実際の音源長 (ms) に合わせて閉じる。取得できなければ60秒で保険クローズ
        _, length = _mci(f"status {alias} length")
        try:
            close_after = int(length) / 1000 + 5
        except (ValueError, TypeError):
            close_after = 60
        err, _ = _mci(f"play {alias} from 0")
        if err:
            display.log_system(
                f"音源再生失敗 ({_mci_error_text(err)}): {path.name}、内蔵ビープで代替",
                display.YELLOW)
            _mci(f"close {alias}")
            return False
        threading.Timer(close_after, lambda: _mci(f"close {alias}")).start()
        return True
    except Exception as e:
        display.log_system(f"音源再生失敗 ({type(e).__name__}): {path.name}",
                           display.YELLOW)
        return False


def _beep_pattern(pattern: list[tuple[int, int]], repeat: int) -> None:
    if winsound is None:
        return
    for _ in range(repeat):
        for freq, ms in pattern:
            try:
                winsound.Beep(freq, ms)
            except RuntimeError:
                return


_PATTERNS = {
    # 警報: 高音を強く繰り返す
    "warning": ([(880, 180), (1175, 180), (880, 180), (1175, 180)], 3),
    # 予報: 2 音 × 2
    "forecast": ([(660, 150), (880, 200)], 2),
    # 津波警報: 低音の長い繰り返し
    "tsunami": ([(523, 400), (392, 400)], 3),
    # 津波注意報: 中程度のチャイム (info より明確に強く、警報よりは控えめ)
    "tsunami_watch": ([(523, 300), (659, 300)], 2),
    # 確定情報など: 短い 1 音
    "info": ([(740, 180)], 1),
}


def _beep_for(kind: str) -> None:
    pattern, repeat = _PATTERNS.get(kind, _PATTERNS["info"])
    _beep_pattern(pattern, repeat)


def play_sound(kind: str, enabled: bool = True) -> None:
    """kind: 'warning' | 'forecast' | 'info' | 'tsunami' | 'tsunami_watch'"""
    if not enabled:
        return
    # カスタム音源があればそれを再生 (WAV/MP3)。MCI 失敗時は内蔵ビープに退避
    custom = _sound_files.get(kind)
    if custom is not None:
        def _run():
            if not _play_file(custom):
                _beep_for(kind)
        threading.Thread(target=_run, daemon=True).start()
        return
    if winsound is None:
        return
    threading.Thread(target=_beep_for, args=(kind,), daemon=True).start()


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


def notify_eew(ev: EEWEvent, cfg: dict, reason: str, home_est=None,
               in_warn_area: bool | None = None) -> None:
    """EEW をトースト+音で通知する。reason: 'new' | 'upgrade' | 'final' | 'cancel'

    home_est: estimate.HomeEstimate (あれば自宅予想震度・到達秒数を本文に含める)
    in_warn_area: 自宅都道府県が警報対象地域か (不明は None)
    """
    ncfg = cfg.get("notify", {})
    sound_on = ncfg.get("sound_enabled", True)
    toast_on = ncfg.get("toast_enabled", True)

    kind = "警報" if ev.is_warn else "予報"

    if reason == "cancel":
        toast(f"【取消】緊急地震速報({kind})", f"{ev.hypocenter} の速報は取り消されました",
              urgent=False, enabled=toast_on)
        play_sound("info", sound_on)
        return

    # 自分事 (この場所がどうなるか) を先頭に置く。トーストは約2行で切り詰められるため、
    # 末尾の震源情報が消えても行動判断に必要な情報が残る並びにする
    body_parts = []
    if in_warn_area:
        body_parts.append("この地域は警報対象です")
    if home_est is not None:
        body_parts.append(f"自宅予想震度 {home_est.intensity_label}(暫定)")
        remaining = home_est.s_remaining(now_jst())
        if remaining is not None and remaining > 0:
            body_parts.append(f"S波まで約{int(remaining)}秒")
    if ev.hypocenter:
        body_parts.append(f"震源: {ev.hypocenter}")
    if ev.is_assumption:
        body_parts.append("震源仮定(PLUM)")  # M はダミー値なので出さない
    elif ev.magnitude is not None:
        body_parts.append(f"M{ev.magnitude:.1f}")
    if ev.max_intensity:
        body_parts.append(f"最大震度 {ev.max_intensity}")
    body = " / ".join(body_parts)

    title_prefix = {"new": "", "upgrade": "【更新】", "final": "【最終報】"}.get(reason, "")
    toast(f"{title_prefix}緊急地震速報({kind})", body, urgent=ev.is_warn, enabled=toast_on)

    if reason in ("new", "upgrade"):
        play_sound("warning" if ev.is_warn else "forecast", sound_on)
