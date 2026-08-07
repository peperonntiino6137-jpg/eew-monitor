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
  - **EEW が出ない地震も表示**: 確定の地震情報（P2P 551）を震央マーカー（橙✕）＋
    パネルで10分間表示（進行中の EEW があればそちらが優先）
  - Leaflet 同梱・OSMタイル。WS自動再接続
- **震度3以上の専用音** (`notify.sounds` の `home_int3` / `national_int3`)
  - 自宅に震度3以上が到達予定 → `震度3以上到達予定.wav`（予報の通知音を置き換え。警報音は従来通り）
  - 全国どこかで震度3以上・自宅は3未満 → `全国震度3以上.wav`（通知対象外の遠方でもトーストなしで音のみ、イベントごとに一度）。確定の地震情報（P2P 551）の音も info からこちらに変更
- **実際の地震の永続ログ**（`logs/events/YYYY-MM.jsonl`）
  - 受信した EEW 全報・確定情報（観測点別震度つき）・津波予報・揺れ検知を JSONL で記録
  - `python -m eew.history` で後からイベント単位に集約して一覧・詳細表示（下記）
  - eew.log と違いローテーションで消えない。テスト（test_alert / demo）は記録されない
- **最大震度3以上の地震の地図画面を自動収録**（`logs/震源地_マグニチュード_最大震度.webm`）
  - Playwright + ローカルブラウザ（Chrome → Edge の順で自動選択）のヘッドレスで
    map.html を裏で開いて録画（画面には何も出ない）
  - 条件を満たした報から収録開始、続報が途絶えて60秒後（S波到達までは延長）または上限5分で保存
  - **EEW が出ない地震も収録**: 確定情報で震度3以上なら受信時点から45秒間収録
    （同じ地震を EEW 側で収録済みなら重複せず、確定値でファイル名だけ更新）
  - 初回のみ `python -m playwright install ffmpeg` が必要（Chrome/Edge があればブラウザの追加DLは不要）
- **強震モニタ画像解析（EEW発表前の揺れ検知）**
  - 全国1628観測点のピクセル色をHSV連続変換式でリアルタイム震度化
  - 誤検知対策: 震度2.5以上 × 半径50km内3点以上 × 2フレーム連続 + クールダウン
- **Windows起動時の自動常駐**（スタートアップ登録、`--headless` でログファイル出力）

## システム要件

- **OS**: Windows 10 / 11（トースト通知・警報音・位置情報・スタートアップ登録が Windows 前提）
- **Python**: 3.11 以上を推奨（動作確認は 3.14）
- **ブラウザ**: 地図表示用に任意のモダンブラウザ。地図収録には **Chrome または Edge**
  （Chrome → Edge の順で自動選択。どちらも無い場合は
  `python -m playwright install chromium` で代替可）
- **ネットワーク**: 3つのデータソースへの常時接続＋NICT 時刻同期（NTP, UDP 123）
- **ポート**: `127.0.0.1` の **8720**（HTTP）/ **8721**（WebSocket）を使用（外部には公開しない。
  デモは 28720/28721 を使用）
- **電源設定**: スリープ中は一切受信できない（復帰時は全ソース自動再接続）。
  常時監視するならスリープ無効を推奨: `powercfg /change standby-timeout-ac 0`

### 初回セットアップ

```powershell
pip install -r requirements.txt
python -m playwright install ffmpeg   # 地図収録用の動画エンコーダ (初回のみ)
copy config.example.json config.json  # 必要に応じて編集 (無ければデフォルトで起動)
```

## 使い方

### 起動方法

```powershell
python -m eew.main                # 前面起動: コンソールに受信ログを表示 (Ctrl+C で終了)
python -m eew.main --headless     # 常駐向け: 出力を logs/eew.log へ (画面なし)
.\scripts\install_startup.ps1     # Windows 起動時の自動常駐を登録
.\scripts\uninstall_startup.ps1   # 自動常駐を解除
```

- スタートアップ登録は `pythonw.exe -m eew.main --headless`（コンソールなし）の
  ショートカットを作る。常駐の停止はタスクマネージャで pythonw を終了
- 多重起動はポート 8720 のバインドで自動的に防止される（2つ目は起動エラーで終了）

### 地図の開き方

- **手動**: 起動中ならいつでも `http://127.0.0.1:8720/map.html` をブラウザで開く
- **起動時に自動**: 前面起動では自動で開く（`map.open_on_start`）。
  headless 常駐では起動のたびには開かない
- **警報時に自動**: 警報・自宅の予想震度がしきい値以上・自宅地域が対象の津波予報の際、
  「画面に見えている地図タブが1つもなければ」自動で新しいタブを開く（`map.open_on_warning`）。
  背面タブしかない場合も開く。60秒のデバウンスでタブは増殖しない
- 地図の**右クリック（長押し）で自宅位置を設定**できる（config.json に保存され、以後自動測位しない）
- WebSocket は3秒間隔で自動再接続するため、開きっぱなしで問題ない

### デモの回し方（実際の発報シーケンスを再現）

```powershell
python -m eew.demo                  # 三陸沖 M8.8 深さ10km (海溝型)
python -m eew.demo 9.4 30           # マグニチュード・深さを指定
python -m eew.demo 7.9 25 sagami    # 震源プリセットを指定
python -m eew.demo rensa            # 複数地震の連発シナリオ
python -m eew.demo info             # EEWなし・確定情報のみの地震 (能登 M5.4 震度4)
```

- 震源プリセット: `sanriku`（三陸沖）/ `nankai`（紀伊半島南東沖）/ `sagami`（相模湾）/
  `shuto`（東京23区直下）/ `hanshin`（淡路島北部）/ `noto`（能登半島沖）/ `kumamoto`（熊本）
- 連発シナリオ: `double`（南海トラフ+三陸沖）/ `triple`（+相模湾）/ `rensa`（歴史的大地震の6連鎖）
- 本番常駐（8720）とは**別ポート 28720/28721** で動くため、監視を止めずに実行できる
- ブラウザは最初は開かず「警報」発表の瞬間に自動で開く — 実際の挙動と同じ。
  プリセットによっては津波予報（大津波警報〜注意報）も再現される
- デモ・テストのイベントは永続ログ・動画収録には記録されない

### テスト（通知・音・地図の動作確認）

```powershell
python -m eew.test_alert        # 予報 → 警報格上げ → 最終報 + カウントダウン
python -m eew.test_alert warn   # いきなり警報
python -m eew.test_alert map    # 地図サーバも起動してブラウザで描画確認
```

`map` モードは本番と同じポート 8720 を使うため、常駐を止めてから実行する
（常駐を止めたくない場合は別ポートで動くデモを使う）。

### 過去の地震の振り返り

```powershell
python -m eew.history                # 直近20件を時系列で一覧
python -m eew.history --limit 0      # 全件
python -m eew.history --days 7       # 直近7日分
python -m eew.history --event <ID>   # 1イベントの全報を詳細表示 (IDは一覧末尾に表示)
python -m eew.history --json         # 機械可読出力 (検証・分析用)
```

震度3以上の地震は地図画面の収録動画（`logs/*.webm`）もあわせて確認できる。

## 設定 (config.json)

```jsonc
{
  "notify": {
    "forecast_min_intensity": "3",  // このしきい値未満の予報は通知しない
    "use_home_intensity": true,     // 自宅予想震度でフィルタ (遠方はトーストせず全国音のみ)
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
  "recording": {
    "enabled": true,                // 最大震度3以上の地震の地図画面を動画収録
    "min_intensity": "3",
    "max_seconds": 300              // 1収録の上限秒数
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
  eventlog.py        実際の地震の永続ログ (logs/events/ に JSONL 追記)
  history.py         永続ログの閲覧 CLI (python -m eew.history)
  recorder.py        地図画面の動画収録 (震度3以上、Playwright + Chrome/Edge ヘッドレス)
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
