"""地図画面の動画収録 (最大震度3以上の地震)。

Playwright + ローカルのブラウザ (Chrome -> Edge -> 同梱 chromium の順) で
map.html をヘッドレスに開き、イベント終息まで録画して
logs/ に「震源地_マグニチュード_最大震度.webm」で保存する。

- main.py が configure() を呼んだときだけ有効 (test_alert / demo では収録しない)
- 収録ページは ?rec=1 付きで開き、地図側は「見えていないタブ」として振る舞う
  (警報時のブラウザ自動オープンを妨げないため)
- 終了条件: 最終報・続報が途絶えて TAIL_SEC 経過 (カウントダウン中は延長) /
  上限 max_seconds
"""
from __future__ import annotations

import asyncio
import re
import shutil
import time
from pathlib import Path

from . import display
from .models import intensity_rank

ROOT = Path(__file__).resolve().parent.parent

TAIL_SEC = 60          # 続報・カウントダウンが途絶えてから収録を続ける秒数
STOP_TAIL_SEC = 3      # 明示停止 (取消など) 後に映像へ残す猶予
VIEWPORT = {"width": 1280, "height": 720}
MAX_CONCURRENT = 3     # 同時収録の上限 (ブラウザプロセス数の暴走防止)

_url: str | None = None
_out_dir: Path | None = None
_min_rank: int | None = None   # None = 無効
_max_sec: float = 300.0
_active = 0

_INVALID_CHARS = re.compile(r'[\\/:*?"<>|\s]+')


def configure(cfg: dict, map_url: str | None) -> None:
    """main.py から呼ばれる。呼ばれなければ収録は一切行わない。"""
    global _url, _out_dir, _min_rank, _max_sec
    rcfg = cfg.get("recording", {}) or {}
    if not rcfg.get("enabled", True):
        return
    if map_url is None:
        display.log_system("地図収録: 地図サーバが無効のため収録しません",
                           display.YELLOW)
        return
    try:
        import playwright  # noqa: F401
    except ImportError:
        display.log_system(
            "地図収録: playwright 未導入のため収録しません "
            "(pip install playwright && python -m playwright install ffmpeg)",
            display.YELLOW)
        return
    _url = map_url + ("&" if "?" in map_url else "?") + "rec=1"
    _out_dir = ROOT / "logs"
    _min_rank = intensity_rank(rcfg.get("min_intensity", "3"))
    _max_sec = float(rcfg.get("max_seconds", 300))
    display.log_system(
        f"地図収録: 有効 (最大震度{rcfg.get('min_intensity', '3')}以上 -> logs/)")


class Recording:
    """1 イベント分の収録。aggregator が touch()/update_meta() で延命・命名する。"""

    def __init__(self, event_id: str, duration: float | None = None):
        self.event_id = event_id
        self.hypocenter = ""
        self.magnitude: float | None = None
        self.intensity_label = ""
        self.cancelled = False
        self.duration = duration   # None なら max_seconds / TAIL_SEC で判定
        self._last_activity = time.monotonic()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def touch(self) -> None:
        self._last_activity = time.monotonic()

    def update_meta(self, hypocenter: str | None = None,
                    magnitude: float | None = None,
                    intensity_label: str | None = None) -> None:
        if hypocenter:
            self.hypocenter = hypocenter
        if magnitude is not None:
            self.magnitude = magnitude
        if intensity_label:
            self.intensity_label = intensity_label

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ run

    def _final_path(self) -> Path:
        hypo = _INVALID_CHARS.sub("", self.hypocenter) or "震源不明"
        mag = f"M{self.magnitude:.1f}" if self.magnitude is not None else "M不明"
        label = self.intensity_label or "不明"
        base = f"{hypo}_{mag}_震度{label}" + ("_取消" if self.cancelled else "")
        path = _out_dir / f"{base}.webm"
        n = 2
        while path.exists():  # 同名地震の上書き防止
            path = _out_dir / f"{base}_{n}.webm"
            n += 1
        return path

    async def _run(self) -> None:
        global _active
        _active += 1
        # event_id に ":" 等が入ることがある (ISO時刻由来) のでディレクトリ名を無害化
        tmpdir = _out_dir / ".rec" / _INVALID_CHARS.sub("-", self.event_id)
        try:
            from playwright.async_api import async_playwright
            tmpdir.mkdir(parents=True, exist_ok=True)
            pw = await async_playwright().start()
            try:
                # Chrome -> Edge -> playwright 同梱 chromium の順に試す
                browser = None
                for channel in ("chrome", "msedge", None):
                    try:
                        browser = await pw.chromium.launch(channel=channel,
                                                           headless=True)
                        break
                    except Exception:
                        continue
                if browser is None:
                    raise RuntimeError(
                        "起動できるブラウザがありません "
                        "(python -m playwright install chromium で導入可)")
                display.log_system(
                    f"地図収録を開始 ({self.event_id}, {channel or 'chromium'})")
                ctx = await browser.new_context(
                    viewport=VIEWPORT,
                    record_video_dir=str(tmpdir),
                    record_video_size=VIEWPORT)
                page = await ctx.new_page()
                await page.goto(_url, wait_until="load")
                start = time.monotonic()
                while True:
                    try:
                        await asyncio.wait_for(self._stop.wait(),
                                               timeout=2.0)
                        await asyncio.sleep(STOP_TAIL_SEC)
                        break
                    except asyncio.TimeoutError:
                        pass
                    now = time.monotonic()
                    if now - start > (self.duration or _max_sec):
                        break
                    if self.duration is None \
                            and now - self._last_activity > TAIL_SEC:
                        break
                await ctx.close()  # ここで動画がフラッシュされる
                video_path = Path(await page.video.path())
                await browser.close()
            finally:
                await pw.stop()
            final = self._final_path()
            shutil.move(video_path, final)
            display.log_system(f"地図収録を保存: {final.name}", display.GREEN)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            display.log_system(f"地図収録失敗 ({type(e).__name__}: {e})",
                               display.YELLOW)
        finally:
            _active -= 1
            shutil.rmtree(tmpdir, ignore_errors=True)


def maybe_start(event_id: str, max_intensity_rank: int,
                duration: float | None = None) -> Recording | None:
    """条件を満たせば収録を開始する。満たさなければ None (呼び出し側で再試行可)。

    duration: 指定すると固定長で収録 (EEW を伴わない確定情報など静的な場面用)。
    """
    if _min_rank is None or max_intensity_rank < _min_rank:
        return None
    if _active >= MAX_CONCURRENT:
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None  # イベントループ外 (テスト等)
    rec = Recording(event_id, duration)
    rec._task = loop.create_task(rec._run(), name=f"record-{event_id}")
    return rec
