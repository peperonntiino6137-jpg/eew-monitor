"""自宅地点の測位。

優先順位:
  1. manual / map  ... ユーザーが確定した座標 (そのまま使う)
  2. windows       ... Windows 位置情報サービス (WiFi測位/GPS、精度数十m)
  3. ip            ... IP ジオロケーション (プロバイダ位置になるため誤差大、最終手段)

home.method に採用した方法を記録する。地図クリックで上書きすると "map" になる。
"""
from __future__ import annotations

import asyncio

import httpx

from . import display

# ユーザーが確定した座標。自動測位で上書きしない
FIXED_METHODS = ("manual", "map")


async def _locate_windows() -> tuple[float, float, float] | None:
    """Windows 位置情報サービスで測位。(lat, lon, accuracy_m) / 不可なら None。"""
    try:
        from winrt.windows.devices.geolocation import (
            Geolocator, GeolocationAccessStatus, PositionAccuracy)
    except ImportError:
        return None
    try:
        status = await Geolocator.request_access_async()
        if status != GeolocationAccessStatus.ALLOWED:
            display.log_system(
                "Windows位置情報が許可されていません "
                "(設定 > プライバシーとセキュリティ > 位置情報 を有効化すると精度向上)",
                display.YELLOW)
            return None
        geo = Geolocator()
        geo.desired_accuracy = PositionAccuracy.HIGH
        pos = await asyncio.wait_for(geo.get_geoposition_async(), timeout=15)
        p = pos.coordinate.point.position
        return p.latitude, p.longitude, float(pos.coordinate.accuracy)
    except Exception as e:
        display.log_system(f"Windows位置情報の取得失敗 ({type(e).__name__})", display.YELLOW)
        return None


async def _locate_ip() -> tuple[float, float, str] | None:
    """IP ジオロケーション。(lat, lon, city) / 不可なら None。"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "http://ip-api.com/json/?fields=status,lat,lon,city,regionName")
            d = r.json()
        if d.get("status") == "success":
            return d["lat"], d["lon"], d.get("city") or d.get("regionName") or ""
    except Exception:
        pass
    return None


async def resolve_home(cfg: dict, save: callable) -> None:
    """自宅座標を決定して cfg['home'] に反映する。save(cfg) で永続化。"""
    home = cfg.setdefault("home", {})
    method = home.get("method") or ""
    has_coords = (home.get("latitude") is not None
                  and home.get("longitude") is not None)

    if has_coords and method in FIXED_METHODS:
        display.log_system(
            f"自宅地点: {home.get('name') or '(名称未設定)'} "
            f"({home['latitude']:.3f}, {home['longitude']:.3f}) [{method}]")
        return

    if not home.get("auto_locate", True):
        if has_coords:
            display.log_system(
                f"自宅地点: ({home['latitude']:.3f}, {home['longitude']:.3f})")
        else:
            display.log_system("自宅地点未設定: 予想震度・カウントダウンは無効",
                               display.YELLOW)
        return

    # --- Windows 位置情報 (毎回測位し直す。移動にも追従) ---
    win = await _locate_windows()
    if win is not None:
        lat, lon, acc = win
        # ここに来る時点で method は manual/map でない = 名前も自動生成値なので上書き
        home.update({"latitude": round(lat, 4), "longitude": round(lon, 4),
                     "method": "windows", "name": "自宅"})
        save(cfg)
        display.log_system(
            f"自宅地点を測位: ({lat:.4f}, {lon:.4f}) 精度±{acc:.0f}m [Windows位置情報] "
            f"※地図クリックでも修正できます", display.GREEN)
        return

    # --- IP フォールバック ---
    if has_coords:
        # 過去の測位結果があるならそれを使う (IP で上書きしない)
        display.log_system(
            f"自宅地点: {home.get('name') or ''} "
            f"({home['latitude']:.3f}, {home['longitude']:.3f}) "
            f"[{method or '前回値'}] ※地図クリックで修正できます", display.YELLOW)
        return
    ip = await _locate_ip()
    if ip is not None:
        lat, lon, city = ip
        home.update({"latitude": lat, "longitude": lon, "method": "ip",
                     "name": city or "自宅"})
        save(cfg)
        display.log_system(
            f"自宅地点をIPから推定: {city} ({lat:.3f}, {lon:.3f}) "
            f"※誤差が大きいため、地図クリックか config.json での修正を推奨",
            display.YELLOW)
    else:
        display.log_system("自宅地点の自動取得失敗: 予想震度は無効", display.YELLOW)
