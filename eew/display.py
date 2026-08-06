"""コンソール表示。ANSI カラーでソース別・重要度別に色分けする。

headless モード時は enable_headless() でローテーションログファイルへ切り替え
(ANSI エスケープは除去)。
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
from pathlib import Path

from .models import EEWEvent, intensity_rank, now_jst

# Windows コンソールで ANSI エスケープを有効化
# (pythonw ではコンソールが無く os.system が一瞬ウィンドウを生むためスキップ)
if sys.stdout is not None and hasattr(sys.stdout, "isatty") and sys.stdout.isatty():
    os.system("")

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_logger: logging.Logger | None = None


def enable_headless(log_path: str | Path) -> None:
    """出力先をローテーションログファイルに切り替える (5MB x 3世代)。"""
    global _logger
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("eew")
    logger.setLevel(logging.INFO)
    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    _logger = logger


def _emit(text: str) -> None:
    if _logger is not None:
        _logger.info(_ANSI_RE.sub("", text))
    else:
        print(text, flush=True)

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
BLUE = "\x1b[34m"
MAGENTA = "\x1b[35m"
CYAN = "\x1b[36m"
WHITE_ON_RED = "\x1b[97;41m"

_SOURCE_COLOR = {
    "wolfx": CYAN,
    "kmoni": GREEN,
    "p2p": MAGENTA,
}


def _ts() -> str:
    return now_jst().strftime("%H:%M:%S.%f")[:-3]


def _src(source: str) -> str:
    color = _SOURCE_COLOR.get(source, "")
    return f"{color}[{source:5s}]{RESET}"


def log(source: str, message: str, color: str = "") -> None:
    _emit(f"{DIM}{_ts()}{RESET} {_src(source)} {color}{message}{RESET}")


def log_system(message: str, color: str = DIM) -> None:
    _emit(f"{DIM}{_ts()}{RESET} {BLUE}[sys  ]{RESET} {color}{message}{RESET}")


def show_eew(ev: EEWEvent, is_new: bool) -> None:
    """EEW 1 報の整形表示。"""
    kind = "警報" if ev.is_warn else "予報"
    kind_col = WHITE_ON_RED if ev.is_warn else YELLOW

    parts = [f"{kind_col}緊急地震速報({kind}){RESET}"]
    if ev.is_cancel:
        parts.append(f"{BOLD}{RED}【取消】{RESET}")
    parts.append(f"第{ev.serial}報" + ("(最終)" if ev.is_final else ""))
    if ev.hypocenter:
        parts.append(f"震源:{ev.hypocenter}")
    if ev.is_assumption:
        parts.append("震源仮定(PLUM)")  # M・深さはダミー値なので出さない
    else:
        if ev.magnitude is not None:
            parts.append(f"M{ev.magnitude:.1f}")
        if ev.depth_km is not None:
            parts.append(f"深さ{ev.depth_km}km")
    if ev.max_intensity:
        # 重大な報を流れるログから一目で拾えるよう震度で色を変える
        rank = intensity_rank(ev.max_intensity)
        if rank >= 5:
            int_col = WHITE_ON_RED
        elif rank >= 3:
            int_col = BOLD + YELLOW
        else:
            int_col = BOLD
        parts.append(f"{int_col}最大震度 {ev.max_intensity}{RESET}")

    lat = ev.latency_ms()
    if lat is not None:
        parts.append(f"{DIM}(遅延{lat}ms){RESET}")

    marker = f"{BOLD}{RED}[新規]{RESET} " if is_new else ""
    log(ev.source, marker + " ".join(parts))

    if ev.is_warn and ev.warn_areas:
        log(ev.source, f"  警報対象: {'、'.join(ev.warn_areas[:12])}"
            + (" ほか" if len(ev.warn_areas) > 12 else ""), YELLOW)


def banner() -> None:
    _emit(f"""{BOLD}{CYAN}
╔══════════════════════════════════════════════════╗
║   緊急地震速報 マルチソース受信モニタ            ║
║   Wolfx / 強震モニタ / P2P地震情報               ║
╚══════════════════════════════════════════════════╝{RESET}
  Ctrl+C で終了
""")
    # pythonw では sys.stdout が None (--headless 付け忘れでも落とさない)
    if _logger is None and sys.stdout is not None and not sys.stdout.isatty():
        log_system("非TTY出力: カラー表示が崩れる場合があります")
