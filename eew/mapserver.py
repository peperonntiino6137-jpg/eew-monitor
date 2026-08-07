"""ローカル地図サーバ。

  http://127.0.0.1:<http_port>/map.html  ... Leaflet 地図 (静的配信, 別スレッド)
  ws://127.0.0.1:<ws_port>               ... イベントのプッシュ配信 (asyncio)

HTTP ポートのバインド失敗を単一インスタンスガードとして流用する。
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import websockets

from . import display
from .models import intensity_rank, now_jst

WEB_DIR = Path(__file__).resolve().parent / "web"

OPEN_DEBOUNCE_SEC = 60     # ブラウザ起動〜WS接続完了前の続報でタブを増やさない
REPLAY_MAX_AGE_SEC = 600   # これより古い EEW は新規接続クライアントに再送しない
TSUNAMI_REPLAY_MAX_AGE_SEC = 3600  # 津波予報は長時間有効なので長めに再送する
QUAKE_INFO_REPLAY_MAX_AGE_SEC = 600  # 確定情報の再送期限 (収録用ページの後追い接続向け)


class AlreadyRunningError(Exception):
    """ポートが既に使用中 = 別インスタンスが起動している。"""


class _QuietHandler(SimpleHTTPRequestHandler):
    ws_port = 8721  # start() で実際の値に差し替えたサブクラスを使う

    def log_message(self, format, *args):  # noqa: A002 - 基底クラスのシグネチャ
        pass

    def do_GET(self):
        # 静的配信だが ws ポートだけは設定値を動的に返す
        if self.path == "/ws_port.js":
            body = f"window.WS_PORT={self.ws_port};".encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


class MapServer:
    def __init__(self, cfg: dict):
        mcfg = cfg.get("map", {})
        self.http_port = int(mcfg.get("http_port", 8720))
        self.ws_port = int(mcfg.get("ws_port", 8721))
        self.open_on_start = bool(mcfg.get("open_on_start", True))
        self.open_on_warning = bool(mcfg.get("open_on_warning", True))
        # 警報未満でも自宅予想震度がこの階級以上なら地図を開く
        self.open_min_home_rank = intensity_rank(
            cfg.get("notify", {}).get("forecast_min_intensity", "3"))

        home = cfg.get("home", {}) or {}
        self._init_payload = {
            "type": "init",
            "home": {
                "name": home.get("name") or "自宅",
                "latitude": home.get("latitude"),
                "longitude": home.get("longitude"),
            },
        }

        self._clients: set = set()
        self._visible: dict = {}           # ws -> タブが画面に見えているか
        self._httpd: ThreadingHTTPServer | None = None
        self._ws_server = None
        # 進行中の全 EEW を保持 (複数地震の同時進行に対応)
        self._active_eew: dict[str, tuple[dict, float]] = {}  # id -> (payload, monotonic)
        self._last_tsunami: dict | None = None
        self._last_tsunami_at: float = 0.0
        self._last_quake_info: dict | None = None
        self._last_quake_info_at: float = 0.0
        self._last_open_at: float = -1e9   # タブ増殖防止デバウンス (monotonic)
        self._send_tasks: set = set()      # 送信タスクの強参照 (GC回収対策)
        # 地図クリックで自宅を設定したときのコールバック (main が設定)
        self.on_set_home = None  # Callable[[float, float], None] | None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.http_port}/map.html"

    # ---------------------------------------------------------------- start

    async def start(self) -> None:
        # HTTP (静的配信) — バインド失敗 = 二重起動
        try:
            handler_cls = type("_Handler", (_QuietHandler,), {"ws_port": self.ws_port})
            handler = partial(handler_cls, directory=str(WEB_DIR))
            self._httpd = ThreadingHTTPServer(("127.0.0.1", self.http_port), handler)
        except OSError as e:
            raise AlreadyRunningError(
                f"ポート {self.http_port} が使用中 (既に起動していませんか?)") from e
        threading.Thread(target=self._httpd.serve_forever,
                         name="map-http", daemon=True).start()

        try:
            self._ws_server = await websockets.serve(
                self._ws_handler, "127.0.0.1", self.ws_port,
                # ブラウザからは自ページのみ許可。Origin ヘッダなし (CLI等) は許可
                origins=[f"http://127.0.0.1:{self.http_port}",
                         f"http://localhost:{self.http_port}", None])
        except OSError as e:
            # headless 時に生のトレースバックで黙って落ちないよう明示エラーにする
            self._httpd.shutdown()
            raise AlreadyRunningError(
                f"WSポート {self.ws_port} が使用中 (既に起動していませんか?)") from e
        display.log_system(f"地図サーバ起動: {self.url}", display.GREEN)

        # 地図の時計用に日本標準時 (NICT補正済み) を定期配信
        self._time_task = asyncio.get_running_loop().create_task(
            self._time_broadcast_loop(), name="map-time")

        if self.open_on_start:
            self.open_browser(force=True)

    async def _time_broadcast_loop(self) -> None:
        while True:
            if self._clients:
                self.broadcast({"type": "time",
                                "server_now": now_jst().isoformat()})
            await asyncio.sleep(15)

    async def _ws_handler(self, ws) -> None:
        self._clients.add(ws)
        try:
            await ws.send(json.dumps(
                {**self._init_payload, "server_now": now_jst().isoformat()},
                ensure_ascii=False))
            # 進行中の全 EEW を再送 (鮮度内のみ。古い速報を「現在」と誤認させない)
            # server_now は発行時点の値のままだとクライアント時計を巻き戻すため現在時刻に差し替える
            now = time.monotonic()
            for payload, ts in sorted(self._active_eew.values(), key=lambda x: x[1]):
                if now - ts < REPLAY_MAX_AGE_SEC:
                    await ws.send(json.dumps(
                        {**payload, "server_now": now_jst().isoformat()},
                        ensure_ascii=False))
            # 発表中の津波予報も再送 (津波で開いたタブにバナーを出すため)
            if (self._last_tsunami is not None
                    and time.monotonic() - self._last_tsunami_at
                    < TSUNAMI_REPLAY_MAX_AGE_SEC):
                await ws.send(json.dumps(self._last_tsunami, ensure_ascii=False))
            # 直近の確定情報も再送 (収録用ページは発表後に接続してくるため必須)
            # server_now はクライアント時計を巻き戻さないよう現在時刻に差し替える
            if (self._last_quake_info is not None
                    and time.monotonic() - self._last_quake_info_at
                    < QUAKE_INFO_REPLAY_MAX_AGE_SEC):
                await ws.send(json.dumps(
                    {**self._last_quake_info,
                     "server_now": now_jst().isoformat()},
                    ensure_ascii=False))
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") == "set_home":
                    self._handle_set_home(msg)
                elif msg.get("type") == "visibility":
                    self._visible[ws] = bool(msg.get("visible"))
        finally:
            self._clients.discard(ws)
            self._visible.pop(ws, None)

    def _handle_set_home(self, msg: dict) -> None:
        lat, lon = msg.get("latitude"), msg.get("longitude")
        if not (isinstance(lat, (int, float)) and isinstance(lon, (int, float))):
            return
        if not (20.0 <= lat <= 46.0 and 122.0 <= lon <= 154.0):  # 日本周辺のみ
            return
        if self.on_set_home is not None:
            self.on_set_home(float(lat), float(lon))
        self._init_payload["home"]["latitude"] = float(lat)
        self._init_payload["home"]["longitude"] = float(lon)
        self.broadcast(self._init_payload)  # 全クライアントの自宅マーカーを更新

    # ------------------------------------------------------------- publish

    def broadcast(self, payload: dict) -> None:
        """Aggregator から同期呼び出しされる。同一イベントループ上で送信タスク化。"""
        if payload.get("type") == "eew":
            eid = str(payload.get("event_id") or "")
            now = time.monotonic()
            if payload.get("is_cancel"):
                self._active_eew.pop(eid, None)
            else:
                self._active_eew[eid] = (payload, now)
            # 期限切れイベントを間引く
            self._active_eew = {k: v for k, v in self._active_eew.items()
                                if now - v[1] < REPLAY_MAX_AGE_SEC}
            home_rank = (payload.get("home") or {}).get("rank", -1)
            urgent = (payload.get("is_warn")
                      or home_rank >= self.open_min_home_rank)
            if urgent and not payload.get("is_cancel"):
                self.maybe_open_on_warning()
        elif payload.get("type") == "quake_info":
            self._last_quake_info = payload
            self._last_quake_info_at = time.monotonic()
        elif payload.get("type") == "tsunami":
            if payload.get("cancelled"):
                self._last_tsunami = None
            else:
                self._last_tsunami = payload
                self._last_tsunami_at = time.monotonic()
                # 自宅地域が津波予報の対象 (注意報含む) なら地図を開く
                # (遠地津波など EEW を伴わないケースにも対応)
                if payload.get("home_grade"):
                    self.maybe_open_on_warning()
        if not self._clients:
            return
        msg = json.dumps(payload, ensure_ascii=False)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for ws in list(self._clients):
            task = loop.create_task(self._safe_send(ws, msg))
            self._send_tasks.add(task)          # 強参照を保持 (GCで消えるのを防ぐ)
            task.add_done_callback(self._send_tasks.discard)

    @staticmethod
    async def _safe_send(ws, msg: str) -> None:
        try:
            await ws.send(msg)
        except Exception:
            pass  # 切断済みクライアントは _ws_handler 側で除去される

    # ------------------------------------------------------------- browser

    def open_browser(self, force: bool = False) -> None:
        """ブラウザで地図を開く。接続中クライアントがいればタブ増殖させない。"""
        if not force and self._clients:
            return
        # 起動〜WS接続完了前に警報が来ても maybe_open_on_warning が二重に開かないようにする
        self._last_open_at = time.monotonic()
        threading.Thread(target=self._do_open, daemon=True).start()

    def _do_open(self) -> None:
        try:
            ok = webbrowser.open(self.url)
            if not ok:
                display.log_system(f"ブラウザを開けませんでした: {self.url}", display.YELLOW)
        except Exception as e:
            display.log_system(f"ブラウザ起動失敗 ({type(e).__name__}): {self.url}",
                               display.YELLOW)

    def maybe_open_on_warning(self) -> None:
        """警報/高予想震度時のブラウザ自動オープン。

        接続数ではなく「画面に見えているタブがあるか」で判定する。
        背面に放置されたタブや再接続しただけのタブでは視覚確認できないため、
        見えているタブが 1 つもなければ新しいタブを開く。
        ブラウザ起動〜WS接続完了までの数秒間に続報が来てもタブを増やさないよう、
        一度開いたら OPEN_DEBOUNCE_SEC の間は再オープンしない。
        """
        if not self.open_on_warning:
            return
        if any(self._visible.get(ws) for ws in self._clients):
            return  # 前面で見えているタブがある
        now = time.monotonic()
        if now - self._last_open_at < OPEN_DEBOUNCE_SEC:
            return
        self._last_open_at = now
        self.open_browser(force=True)
