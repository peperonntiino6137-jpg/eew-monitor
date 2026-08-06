# 緊急地震速報 マルチソース受信モニタ

無料の3つのデータソースを**同時受信**し、最速で届いた情報から緊急地震速報（EEW）を
Windows トースト通知＋警報音＋コンソール／ブラウザ地図で知らせる常駐ツール。

## データソース

| ソース | 方式 | 内容 |
|---|---|---|
| [Wolfx API](https://wolfx.jp) | WebSocket 常時接続 | JMA 緊急地震速報（予報・警報） |
| [強震モニタ](http://www.kmoni.bosai.go.jp) | 1秒間隔ポーリング | EEW JSON ＋ リアルタイム震度画像解析（揺れ検知） |
| [P2P地震情報](https://www.p2pquake.net) | WebSocket 常時接続 | EEW警報(556)・EEW検知(554)・地震情報(551)・津波予報(552) |

同一イベントはイベントID（発生時刻ベース）で重複排除し、**最初に届いたソースが通知を発火**、
続報は表示更新のみ。予報→警報への格上げや震度の上方修正時は再通知する。

## 機能

- **現在地点の予想震度・S波到達カウントダウン**
  - 司・翠川(1999) 距離減衰式 + 松岡・翠川(1994) 地盤増幅 + 翠川ほか(1999) 震度換算
  - 毎報再計算で補正。値は暫定・±数秒の目安
  - 現在座標は **Windows 位置情報サービス**（WiFi測位、精度数十m）で自動取得。
    地図の**右クリックで手動設定**も可能（`method: "map"` として保存され以後自動測位しない）
- **ブラウザ地図表示** — `http://127.0.0.1:8720/map.html`（起動時自動オープン）
  - 震央・自宅マーカー、P/S波の広がり同心円、カウントダウン大表示、警報地域
  - Leaflet 同梱・OSMタイル。WS自動再接続
- **強震モニタ画像解析（EEW発表前の揺れ検知）**
  - 全国1628観測点のピクセル色をHSV連続変換式でリアルタイム震度化
  - 誤検知対策: 震度2.5以上 × 半径50km内3点以上 × 2フレーム連続 + クールダウン
- **Windows起動時の自動常駐**（スタートアップ登録、`--headless` でログファイル出力）

## 使い方

```powershell
pip install -r requirements.txt
python -m eew.main              # コンソール表示あり
python -m eew.main --headless   # ログファイルのみ (logs/eew.log)、常駐用
```

### テスト（実際の地震を待たずに確認）

```powershell
python -m eew.test_alert        # 予報 → 警報格上げ → 最終報 + カウントダウン
python -m eew.test_alert warn   # いきなり警報
python -m eew.test_alert map    # 地図サーバも起動してブラウザで描画確認
```

### 自動常駐の登録／解除

```powershell
.\scripts\install_startup.ps1     # スタートアップに登録
.\scripts\uninstall_startup.ps1   # 解除
```

多重起動はポート8720のバインドで自動的に防止される。

## 設定 (config.json)

```jsonc
{
  "notify": {
    "forecast_min_intensity": "3",  // このしきい値未満の予報は通知しない
    "use_home_intensity": true,     // 自宅予想震度でフィルタ (遠方の地震で鳴らさない)
    "toast_enabled": true,
    "sound_enabled": true
  },
  "home": {
    "auto_locate": true,            // Windows位置情報 → IP の順で自動測位
    "latitude": null, "longitude": null,
    "avs30": 300,                   // 地盤の平均S波速度 (J-SHISで調べると予想精度向上)
    "method": ""                    // "manual"/"map" にすると自動測位で上書きされない
  },
  "map": {
    "enabled": true, "http_port": 8720, "ws_port": 8721,
    "open_on_start": true,          // 起動時にブラウザを開く (headless時は警報時のみ)
    "open_on_warning": true
  },
  "kmoni_image": {
    "enabled": true,
    "trigger_intensity": 2.5, "min_points": 3,
    "cluster_km": 50, "cooldown_seconds": 60
  },
  "sources": { "wolfx": true, "kmoni": true, "p2p": true },
  "stale_seconds": 180
}
```

## 構成

```
eew/
  main.py            エントリポイント (asyncio 並行受信、--headless)
  aggregator.py      重複排除・通知判断・カウントダウン・地図プッシュ
  estimate.py        距離減衰式による予想震度・走時計算
  location.py        自宅測位 (Windows位置情報 → IP フォールバック)
  mapserver.py       地図サーバ (HTTP静的配信 + WSプッシュ、単一インスタンスガード)
  models.py          EEWEvent / GroundMotionEvent・震度ユーティリティ
  display.py         コンソール表示 (ANSI) / headless時はローテーションログ
  notifier.py        トースト通知 (winotify) + 警報音 (winsound)
  test_alert.py      通知・カウントダウン・地図のテスト
  sources/
    wolfx.py         Wolfx WebSocket クライアント
    kmoni.py         強震モニタ共有クライアント + EEW JSON ポーリング
    kmoni_image.py   リアルタイム震度画像解析 (揺れ検知)
    p2p.py           P2P地震情報 WebSocket クライアント
  web/               地図UI (Leaflet 同梱)
  data/              強震モニタ観測点リスト (ingen084/kyoshin-monitor-observation-points)
scripts/             スタートアップ登録/解除
```

## 注意

- Wolfx・強震モニタは非公式利用のため、仕様変更・停止の可能性がある（3系統の冗長化はその対策でもある）
- 予想震度・到達時刻は簡易モデルによる暫定値。カウントダウンはPC時計精度に依存する
- 実測検証（2026年7〜8月の80地震・約1.1万観測点との突き合わせ）では、予想震度は
  平均+0.6階級の**安全側（過大）バイアス**を持つ（MAE 0.71階級、±1階級以内が99.6%）。
  主因は震度換算式の低震度域外挿・AVS30固定・点震源近似で、遠距離ほど過大傾向が強い。
  深さ100km超の深発地震（異常震域）は未検証
- 本ツールは補助的な情報提供であり、防災行動は気象庁の公式情報に従うこと
