# moomoo/SBI 短期急騰候補の自動化ガイド

これは自動売買ではなく、短期で大きく動く可能性がある銘柄を毎日自動で絞り込むための運用テンプレートです。最終判断と発注は moomoo または SBI 証券で手動確認してください。

## 推奨構成

1. moomoo でスクリーニング
   - 小型/中型株、価格帯、出来高、相対出来高、上昇率、52週高値接近、決算/ニュースを確認します。
   - moomoo OpenAPI/OpenD を使える場合は、後でデータ取得をAPI化できます。

2. SBI 証券で日本株の通知と発注
   - SBI 株アプリの株価アラート、決算通知、コーポレートアクション通知を使います。
   - SBI 側はスキャン自動化よりも、通知と執行確認に使うのが現実的です。

3. ローカルスコアラーで候補順位付け
   - CSV を `short_term_runner_scanner.py` に渡すと、`ALERT`、`WATCH`、`IGNORE` に分類します。

## CSV項目

必須項目:

- `symbol`
- `market`
- `price`
- `market_cap`
- `volume`
- `avg_volume_20d`
- `change_pct`
- `distance_to_52w_high_pct`
- `gap_pct`
- `catalyst`

任意項目:

- `float_shares`
- `short_interest_pct`

## 実行例

```bash
python3 outputs/short_term_runner_scanner.py outputs/sample_runner_input.csv --alerts-only
```

出力先を指定する場合:

```bash
python3 outputs/short_term_runner_scanner.py outputs/sample_runner_input.csv -o outputs/today_runner_candidates.csv --alerts-only
```

## moomoo OpenAPIで自動取得する

前提:

- moomoo OpenAPI Pythonパッケージをインストールする。
- moomoo OpenDを起動し、ログインする。
- OpenDのQuoteポートを確認する。通常は `127.0.0.1:11111`。

インストール:

```bash
python3 -m pip install -r outputs/requirements.txt
```

米国株をスキャンする例:

```bash
python3 outputs/moomoo_openapi_screener.py --markets US
```

米国株、日本株、香港株をまとめてスキャンする例:

```bash
python3 outputs/moomoo_openapi_screener.py --markets US,JP,HK
```

出力:

- `outputs/moomoo_runner_input.csv`: OpenAPIから取得したスコアラー入力CSV
- `outputs/moomoo_runner_candidates.csv`: `ALERT` / `WATCH` に絞った順位付き候補
- CSVはExcelで開いても文字化けしにくいBOM付きUTF-8で保存されます。

スコアリングせず、OpenAPI取得CSVだけ作る例:

```bash
python3 outputs/moomoo_openapi_screener.py --markets US --no-rank
```

注意:

- moomoo OpenAPIは2026年6月時点で日本株や香港株の相場情報にも対応していますが、利用可否はアカウント、OpenDバージョン、データ権限に依存します。
- 日本株や香港株でAPIエラーが出る場合は、`--markets US` など取得できる市場だけで運用してください。

OpenAPI側では、現在値、時価総額、当日騰落率、量比、52週高値からの距離、平均出来高、浮動株数で一次抽出します。ニュースや決算の「催化」は初版では自動判定しないため、必要なら生成されたCSVの `catalyst` 列に手入力してから `short_term_runner_scanner.py` を再実行してください。

## moomoo OpenAPIでバックテストする

OpenDから日足の歴史K線を取得し、急騰シグナル後の翌営業日寄り付きで入る簡易バックテストを実行できます。

米国候補をバックテストする例:

```bash
python3 outputs/moomoo_backtest_runner.py \
  --symbols-csv outputs/moomoo_runner_candidates.csv \
  --start 2026-01-01 \
  --end 2026-06-10 \
  --trades-output outputs/moomoo_backtest_trades_us.csv \
  --summary-output outputs/moomoo_backtest_summary_us.csv
```

日本候補をバックテストする例:

```bash
python3 outputs/moomoo_backtest_runner.py \
  --symbols-csv outputs/moomoo_runner_candidates_jp.csv \
  --start 2026-01-01 \
  --end 2026-06-10 \
  --trades-output outputs/moomoo_backtest_trades_jp.csv \
  --summary-output outputs/moomoo_backtest_summary_jp.csv
```

香港候補をバックテストする例:

```bash
python3 outputs/moomoo_backtest_runner.py \
  --symbols-csv outputs/moomoo_runner_candidates_hk.csv \
  --start 2026-01-01 \
  --end 2026-06-10 \
  --trades-output outputs/moomoo_backtest_trades_hk.csv \
  --summary-output outputs/moomoo_backtest_summary_hk.csv
```

デフォルト条件:

- シグナル: 当日上昇率 `8%` 以上、相対出来高 `2倍` 以上、52週高値から `-5%` 以内。
- エントリー: シグナル翌営業日の寄り付き。
- 決済: `+30%` 利確、`-10%` 損切り、または `10営業日` 経過。
- スリッページ: エントリー/決済に `0.2%` ずつ加味。

出力:

- `outputs/moomoo_backtest_trades_*.csv`: 取引明細。
- `outputs/moomoo_backtest_summary_*.csv`: 勝率、平均リターン、最大損益、損益係数など。

注意:

- 日足バックテストなので、同日に利確と損切りの両方に到達した場合は保守的に損切り優先です。
- ニュース、決算、板の厚さ、約定可否、空売り規制、実際のスプレッドは完全には再現しません。
- 小型株ではスリッページを `0.5%` 以上に上げて再テストすると、より保守的です。

## スコアの読み方

- `ALERT`: 強い候補。チャート、ニュース、板、出来高を確認して監視。
- `WATCH`: 条件は近いが、追加確認が必要。
- `IGNORE`: 今は短期急騰狙いの優先度が低い。

## 毎日の運用

1. 寄り前または寄り後30分で候補CSVを作る。
2. スコアラーを実行する。
3. `ALERT` のみ moomoo/SBI のウォッチリストに入れる。
4. 価格アラートを3つ設定する。
   - 突破価格
   - 損切り価格
   - 第1利確価格
5. 発注前に必ずニュース、出来高、VWAP、直近高値、売買代金を確認する。

## リスク管理ルール

- 1回の損失は口座全体の `0.5%-1%` 以内。
- 初期損切りは `-7%-12%`、またはVWAP/突破ライン割れ。
- `20%-30%` 上昇で一部利確し、残りで大相場を狙う。
- 流動性が低い銘柄、無ニュース急騰、連続増資銘柄は避ける。
- 自動発注は最初から行わない。最低でも数週間はスキャン結果を記録して精度を確認する。

## 次にAPI化する場合

- moomoo OpenAPI/OpenD で価格、出来高、時価総額、量比、52週高値接近の取得を自動化済みです。
- SBI は通知/発注確認の役割に残し、APIで無理に注文自動化しない方針にします。
- 通知は最初はCSV出力、次にメール/Slack/LINEへ拡張します。

## ポジションモード（中期ブレイクアウト戦略）

`--mode position` は当日急騰の追随ではなく、機関投資家型のトレンドフォロー戦略です。
数週間〜数ヶ月保有して大きな値幅を狙います。runner モードとは別物として運用してください。

### パイプライン

1. ハードゲート（1つでも不合格なら IGNORE）
   - 流動性: 売買代金が市場ごとの下限以上
   - トレンド: 株価 > 50日線 > 200日線、かつ200日線が上向き
   - 52週ポジション: 高値から-25%以内、安値から+30%以上
   - 相対強度: 6ヶ月リターンがベンチマーク（SPY/TOPIX/2800）を上回る
   - 構造リスク: OTC/SPAC/シェルは除外
2. スコアリング（合計100点）
   - 相対強度 25 / ベース品質 20 / 出来高を伴うブレイクアウト 15
   - ファンダメンタルズ（売上成長+加速）25 / 高値接近 10 / 材料 5
3. ステータス
   - `ALERT`: 70点以上かつ当日ブレイクアウト → エントリー検討
   - `WATCH`: 70点未満でブレイクアウト済み → 追加確認
   - `SETUP`: 条件成立だがブレイクアウト待ち → ウォッチリスト入り

### 実行例

```bash
# スクリーニング（日米）
python3 outputs/moomoo_openapi_screener.py --mode position --markets US,JP \
  --input-output outputs/position_input.csv \
  --ranked-output outputs/position_candidates.csv

# 売上データを手動CSVで補完する場合（symbol,revenue_growth_pct,revenue_accel_pp,catalyst）
python3 outputs/moomoo_openapi_screener.py --mode position --markets JP \
  --fundamentals-csv outputs/fundamentals_jp.csv

# バックテスト（2〜3年、ウォークフォワード分割つき）
python3 outputs/moomoo_backtest_runner.py --strategy position \
  --symbols-csv outputs/position_candidates.csv \
  --start 2023-01-01 --end 2026-06-01 \
  --benchmark US.SPY --split-date 2025-06-01 \
  --trades-output outputs/position_backtest_trades.csv \
  --summary-output outputs/position_backtest_summary.csv
```

### ポジションモードの退出ルール（バックテスト既定値）

- ハードストップ: エントリーから `-15%`
- トレーリング: 終値が50日線を下回ったら翌日扱いで決済
- 期限: 最大60営業日
- 固定利確はなし（トレンドに乗せて伸ばす。`--position-take-profit-pct` で有効化可能）
- 同一銘柄の重複エントリーは禁止（前のトレード決済まで新規シグナル無視）

### 判定基準

サマリーCSVの `未知期間` セグメントが本当の成績です。検証期間だけ良くて
未知期間が悪い場合はカーブフィッティングを疑い、パラメータ調整をやめてください。
最低100取引たまるまでは勝率・期待値を信用しないこと。

### runner モードとの使い分け

- runner: デイ〜数日のイベントドリブン。当日の急騰銘柄を翌日抜けで狙う。バックテストでは期待値マイナスが確認されているため、運用は非推奨。
- position: 数週間〜数ヶ月のトレンドフォロー。ファンダメンタルズの裏付けがある銘柄のベース突破を狙う。こちらを主力とする。

## マクロ・レジームエンジン（market_regime.py）

トップダウンの「ポジション許可」レイヤー。銘柄選択には一切関与せず、
新規エントリーの可否と上限サイズだけを制御します。

### 3層構造

1. 市場層（日次・市場別）: 指数 vs 200日線、200日線の傾き、広度代理（RSP）、VIX、HY信用スプレッド
   - 不合格0個 = 緑（通常運用）/ 1-2個 = 黄（新規半分・ALERT上位のみ）/ 3個以上 = 赤（新規停止）
2. スロー系（週次・グローバル）: 新規失業保険申請(ICSA)、Sahmルール、逆イールド解消、コアPCE再加速
   - 降格専用。警告1つで1段階、2つ以上で2段階降格。市場層が赤を緑に戻すことはできない
3. イベントカレンダー: FOMC/CPI/雇用統計/日銀の48時間前から新規禁止フラグ
   - 雇用統計は第一金曜日として自動計算。FOMC/日銀は `macro_event_calendar.csv` を
     四半期ごとに公式サイトと照合して更新すること（CPI日程は各自BLSカレンダーから追加）

### 実行

```bash
python3 outputs/market_regime.py --markets US,JP
# 出力: outputs/market_regime.json + コンソールサマリー
# ネット断時はキャッシュで動作し stale フラグが付く: --offline で強制キャッシュモード
```

### 運用ルール

- スクリーナーを回す前に必ず実行し、赤灯の市場では新規エントリーをしない
- exposure_multiplier (1.0/0.5/0.0) をポジションサイズに乗算する
- レジームに関係なく既存ポジションの損切り・トレーリングは継続する
- このエンジンの判定で銘柄スコアを変えてはいけない（帰属分析が壊れる）

## ポジションサイザー（position_sizer.py）

スキャナーのALERTを具体的な株数・損切り価格に変換する最終レイヤー。発注は常に手動。

### ルール（適用順）

1. レジームゲート: 赤灯 = 見送り / イベント48時間内 = 延期 / 黄灯 = リスク予算半減
2. ATRサイジング: 1取引リスク = 口座×1%。ストップ幅 = min(2×ATR, 15%)。
   株数 = リスク予算 ÷ 1株あたりストップ幅（日本株は100株単元に切り捨て）
3. 単一銘柄上限: 評価額の10%
4. テーマ集中度: AI/半導体は既存+新規で40%以内（超過中は新規ブロック）
5. 決算近接: earnings CSV登録時、2日以内なら延期。未登録は「要手動確認」

### 実行

```bash
python3 outputs/market_regime.py --markets US,JP
python3 outputs/position_sizer.py outputs/position_candidates.csv \
  --portfolio-csv outputs/my_portfolio.csv \
  --earnings-csv outputs/earnings_dates.csv
# 出力: outputs/position_plan.csv (執行候補/減額/延期/見送り + 理由)
```

`my_portfolio.csv` は保有銘柄と theme 列（AI/商社/ゴールド/配当/インデックス等）を
評価額が大きく動いたら月1回程度更新する。為替はレジームエンジンのキャッシュ
(DEXJPUS) を自動使用、`--fx-usdjpy` で上書き可。

### 週次ワークフロー（全体）

日曜夜または月曜朝:
1. `market_regime.py` -> 灯色とイベント確認
2. `moomoo_openapi_screener.py --mode position` -> 候補生成
3. `position_sizer.py` -> 発注プラン
4. 執行候補のみ株探/決算短信で10分ずつ手動確認 -> 納得したものだけ発注
5. 約定したら `trade_journal.csv` に記録（thesis と catalyst は必須。
   exit時に lesson を書く。50取引たまったら勝因敗因を集計する）

## 香港市場の追加対応

- レジームエンジン: `--markets US,JP,HK` でハンセン指数(Yahoo)、人民元安定性
  (USDCNY 20営業日で2%以上の元安 = 警告)、グローバルVIX/HY利差を判定
- 為替: HKDJPY はレジームキャッシュ (DEXJPUS ÷ DEXHKUS) から自動算出
- 単元株数: 香港株は銘柄ごとに異なるため moomoo スナップショットの lot_size を
  CSV経由でサイザーまで引き渡す (日本株は100株、米国株は1株のフォールバック)
- イベント: FOMCは `market=ALL` でHKにも適用される (HKDペッグのため)。
  中国の重要統計 (GDP/PMI等) を警戒する場合は calendar CSV に market=HK で追加
- クォータ節約のローテーション例: 第1週 US / 第2週 JP / 第3週 HK / 第4週 予備

## 日足データソースの切替（クォータ対策）

`--bars-source` で日足の取得元を選択:

- `auto`（既定）: Yahoo Finance優先（歴史Kラインクォータを消費しない）、
  失敗・データ不足時のみmoomooにフォールバック
- `yahoo`: Yahooのみ（クォータ完全節約。新規上場直後など一部欠損あり）
- `moomoo`: 従来どおり（クォータ消費。データ品質を最優先する場合）

シンボル変換: US.PRSU→PRSU / JP.7716→7716.T / HK.00700→0700.HK / US.BRK.B→BRK-B

これによりmoomooの歴史Kラインクォータ（無料枠100銘柄/7日）は実質的に
制約でなくなり、毎週3市場フルスキャンが可能。moomooは銘柄フィルタ・
スナップショット・板块情報（いずれもクォータ消費なし）に専念する。

なお口座資産80万円超でもクォータが100のままの場合は、moomoo証券（日本）の
サポートに「歴史Kライン額度の引き上げ条件」を確認すること（国際版は
総資産1万HKD以上で300銘柄と文書化されているが、日本法人は条件が異なる可能性）。
