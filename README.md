# Uniswap V3 LP 収益力モニター

Ethereum / Optimism / Base / Arbitrum / BNB Smart Chain / Unichain の Uniswap V3
ETH/USDC プールから、毎日 `slot0` / `liquidity` / `feeGrowthGlobal` をオンチェーンで
取得し、**単位流動性あたりの手数料（プール規模に依存しない収益力）** を比較する。

DefiLlama 等の外部APIではなく、プールコントラクトが記録する実測値（`feeGrowthGlobal`
の日次増分）を直接使うため、履歴が残り、任意のレンジ幅での試算ができる。

## 仕組み

```
GitHub Actions（毎日 00:10 UTC = 09:10 JST）
  └ collect.py  ── RPC(アーカイブノード) ──▶ 各チェーンのプール
        └ data/<chain>.csv に1行追記 → 自動 commit
index.html（GitHub Pages）── CSV(BigInt) を読んで年率利回り・取引量・流動性を算出/表示
```

## セットアップ（初回のみ）

### 1. リポジトリを作る
このフォルダの中身をそのまま GitHub の新規リポジトリへ push する。

### 2. RPC を Secrets に登録
リポジトリの **Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `RPC_ETHEREUM` | `https://eth-mainnet.g.alchemy.com/v2/<key>` |
| `RPC_OPTIMISM` | `https://opt-mainnet.g.alchemy.com/v2/<key>` |
| `RPC_BASE` | `https://base-mainnet.g.alchemy.com/v2/<key>` |
| `RPC_ARBITRUM` | `https://arb-mainnet.g.alchemy.com/v2/<key>` |
| `RPC_BSC` | `https://bnb-mainnet.g.alchemy.com/v2/<key>` |
| `RPC_UNICHAIN` | `https://unichain-mainnet.g.alchemy.com/v2/<key>` |

未設定のチェーンは自動でスキップされ、他のチェーンの処理は継続する。

**重要: アーカイブノードが必要。** 公式の無料エンドポイント
（例: `https://mainnet.base.org`）は直近の状態しか保持しておらず、過去ブロックへの
`eth_call`（backfill・過去日の再取得）ができない。Alchemy / Infura / QuickNode 等の
アーカイブ対応プランを使うこと。

### 3. 設定を検証する（必須）
```bash
export RPC_BASE=https://...
export RPC_ARBITRUM=https://...
# ...設定した分だけ
python collect.py --verify
```
各チェーンについて、接続可否・factory から検出したプール・手数料ティア・
`token0`/`token1` の decimals・現在の ETH 価格が表示される。

```
■ arbitrum
  接続OK  chainId=42161  最新ブロック=xxx,xxx,xxx
  factory=0x...
     0.05%  0x...  liquidity=1.234e+18
     0.30%  0x...  liquidity=5.678e+17
  → 採用: 0x...  手数料0.05%  WETHはtoken1  (wethDecimals=18, usdcDecimals=6)
  → ETH価格 $2,451.74  ← 実勢と合っていれば設定は正しい
```

**最後に出る ETH 価格が実勢の相場と合っているかどうかで、設定（アドレス・decimals）の
正誤を判断できる。** 大きくずれる場合は `config.json` の `factory`/`weth`/`usdc` を
見直す。特に `bsc` と `unichain` は未検証のため要確認（下記「未検証事項」）。

### 4. GitHub Pages を有効化
**Settings → Pages → Source: Deploy from a branch → main / (root)**
数分後に `https://<user>.github.io/<repo>/` でダッシュボードが見られる。

### 5. 過去データを取る（初回のみ・チェーンごと）
```bash
export RPC_BASE=https://...
python collect.py --backfill 400 base      # base のみ、直近400日分
python collect.py --backfill 400            # 全チェーン、環境変数が設定済みのもの
```
ブロック番号は二分探索ではなく、チェーンごとのブロック間隔（`config.json` の
`block_sec`）から推定して数回で収束させるため高速（例: Base で2,184点を111秒で取得
した実績あり）。同じチェーンに対して再実行しても、既存の日付は上書きされず
（日付キーで重複排除）、他チェーンの処理には影響しない。

`data/base.csv` には Base の実データ 888日分が同梱済み。他チェーンも同様の手順で
`data/<chain>.csv` を作成し、commit すれば以降は Actions が毎日1行ずつ追記する。

## ファイル

| | |
|---|---|
| `config.json` | チェーン・トークン・factory の設定、および `$10,000`/`±20%` の基準値（`position`） |
| `collect.py` | 収集スクリプト（日次／backfill／`--verify` 対応） |
| `.github/workflows/collect.yml` | 毎日自動実行・自動 commit |
| `index.html` | ダッシュボード（Chart.js のみ・ビルド不要） |
| `data/*.csv` | 蓄積データ（`feeGrowthGlobal` は uint256 を10進文字列として保存） |

## 指標の定義

**年率手数料利回り** = 「$10,000 を時価±20%レンジで提供した場合に受け取る手数料」の
30日/90日/全期間合計 ÷ $10,000 × 365/日数

`feeGrowthGlobal`（= 流動性1単位が受け取った手数料の累積値）の日次増分に、その時点の
価格・レンジで $10,000 分の流動性 `L` を掛けて算出する。**プールの TVL や出来高からの
按分ではない** — TVL にはレンジ外で手数料を受け取れない流動性が含まれるため、按分する
と実際の取り分を過大評価する（実測で約1.6倍の乖離を確認済み）。

**取引量（1日あたり）** = `feeGrowthGlobal` の増分 × アクティブ流動性 ÷ 手数料率 で
逆算。Base の実績値（The Graph / Messari）との突合で比率1.087で一致することを検証済み。

**注意**:
- 本指標は手数料の獲得力のみを示す。IL（インパーマネントロス）・スワップコスト・
  成功報酬は含まない。実際の損益はこれらを差し引いた後の値になる。
- 「$10,000 ±20%」は参考シナリオであり、実際の V3 運用ではレンジ設定・値動きにより
  数値は大きく変動する。特定のレンジでの試算はダッシュボード下部の外部シミュレーター
  （Revert Finance / Metrix Finance / Metacrypt / Qalc.ai）を使うこと。
- ダッシュボードの「参考: DefiLlama」列はフルレンジ前提の外部値であり、計算方法が
  異なるため本指標とは一致しない。あくまで参考。
- `feeGrowthGlobal` は稀にオーバーフローで巻き戻ることがある。増分が負になった日は
  自動的にスキップされる。
- トークンの decimals（桁数）はチェーンごとに自動検出しており、18/6桁と決め打ちして
  いない。例えば BNB Smart Chain の USDC は 18桁であり、これをハードコードすると
  ETH価格が10^12倍ずれる。`--verify` の ETH価格表示はこの種の設定ミスを見つけるための
  ものでもある。

## 未検証事項

`config.json` の `bsc` と `unichain` は factory / token アドレスを未検証。
`collect.py` は起動時に factory から pool を検出し、`liquidity > 0` を確認するので、
アドレスが間違っていれば「プールが見つかりません」と出る。また `--verify` が返す
ETH 価格が実勢と大きくずれる場合も、アドレスまたは decimals の設定ミスを疑うこと。
その場合は `config.json` を修正する。

USDC のネイティブ版／ブリッジ版（USDC.e など）が併存するチェーンについては、
`usdc` を配列にすることで両方を候補として登録でき、`collect.py` が自動的に
流動性の大きい方を選択する（Optimism / Arbitrum は設定済み）。他チェーンで追加の
候補アドレスが必要な場合も同様に配列へ追記すること（本リポジトリでは検証できない
アドレスを推測で追加することはしていない）。

## 制約・既知の注意点

- 秘匿情報（RPC URL・APIキー）はリポジトリに含めない。GitHub Secrets を使う。
- RPC 未設定のチェーンはスキップし、他チェーンの収集は継続する。
- プールが存在しない、または `liquidity` が 0 のチェーンではエラーで落とさず
  「プールが見つかりません」として当該チェーンのみスキップする。
