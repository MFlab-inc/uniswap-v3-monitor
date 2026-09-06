# Uniswap V3 LP 収益力モニター

Ethereum / Optimism / Base / Arbitrum / BNB Smart Chain / Unichain の Uniswap V3
プールから、毎日 `slot0` / `liquidity` / `feeGrowthGlobal` をオンチェーンで取得し、
**単位流動性あたりの手数料（プール規模に依存しない収益力）** を比較する。

対象は基本的に ETH/USDC だが、COINPOOL が実運用するプールに合わせて BNB Smart Chain
だけは **BNB/USDT**（原資産が ETH でない参考枠）になっている（詳細は下記「対象チェーン
とペア」）。

DefiLlama 等の外部APIではなく、プールコントラクトが記録する実測値（`feeGrowthGlobal`
の日次増分）を直接使うため、履歴が残り、任意のレンジ幅での試算ができる。

## 仕組み

```
GitHub Actions（毎日 00:10 UTC = 09:10 JST）
  └ collect.py  ── RPC(アーカイブノード) ──▶ 各チェーンのプール
        └ data/<chain>.csv に1行追記 → 自動 commit
index.html（GitHub Pages）── CSV(BigInt) を読んで年率利回り・取引量・流動性を算出/表示
```

## 対象チェーンとペア

| チェーン | ペア | プールの決め方 | 資産クラス |
|---|---|---|---|
| Ethereum | ETH/USDC | factory 探索（全手数料ティア中、流動性最大） | eth |
| Optimism | ETH/USDC | factory 探索（USDC ネイティブ/ブリッジ両対応） | eth |
| Base | ETH/USDC | factory 探索 | eth |
| Arbitrum | ETH/USDC | factory 探索（USDC ネイティブ/ブリッジ両対応） | eth |
| BNB Smart Chain | **BNB/USDT** | **プール直接指定**（`0x6fe9E9de56356F7eDBfcBB29FAB7cd69471a4869`） | **other（参考）** |
| Unichain | ETH/USDC | **プール直接指定**（`0x8927058918e3CFf6F55EfE45A58db1be1F069E49`） | eth |

BSC と Unichain は factory 探索を行わず、`config.json` の `pool` に直接指定された
プールを使う。`token0()`/`token1()`/`fee()`/`decimals()`/`symbol()` をそのプールに
問い合わせて構成を自動判定する（`collect.py` の `inspect_pool()`）。

**BNB/USDT は原資産が ETH ではないため、ETH系のチェーンと同列に並べて比較しては
ならない。** 収益力（単位流動性あたりの手数料）としての比較自体は成立するが、原資産の
ボラティリティが異なりIL特性も変わるため、「どこで ETH を運用するか」という比較には
使えない。`config.json` の `asset_class` で `eth`/`other` を区別しており、ダッシュボード
では `other` の行を淡色表示 + 「参考」タグ + グラフは点線で分離して表示する。

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

### 3. 設定を検証する（必須・最初に必ず実行すること）
```bash
export RPC_ETHEREUM=https://...
export RPC_OPTIMISM=https://...
export RPC_BASE=https://...
export RPC_ARBITRUM=https://...
export RPC_BSC=https://...
export RPC_UNICHAIN=https://...
# 設定した分だけでよい。未設定のチェーンは「環境変数が未設定 → スキップ」と出る。
python collect.py --verify
```

各チェーンについて、接続可否・プール構成（factory探索 or 直接指定）・token0/token1の
シンボルとdecimals・現在の価格が表示される。

```
■ arbitrum  (ETH/USDC)
  接続OK  chainId=42161  最新ブロック=xxx,xxx,xxx
  factory=0x...
     0.05%  0x...  liquidity=1.234e+18
     0.30%  0x...  liquidity=5.678e+17
  → 採用: 0x...  WETH/USDC  手数料0.05%
  → 価格 2,451.744500  (ETH/USDC)  ← 実勢と合っていれば設定は正しい

■ bsc  (BNB/USDT・参考枠(原資産≠ETH))
  接続OK  chainId=56  最新ブロック=xxx,xxx,xxx
    プール直接指定: 0x6fe9E9de56356F7eDBfcBB29FAB7cd69471a4869
    token0=WBNB(18桁) 0x...
    token1=USDT(18桁) 0x...
    手数料0.05%  liquidity=...
  → 価格 612.340000  (BNB/USDT)  ← 実勢と合っていれば設定は正しい
```

**最後に出る価格が実勢の相場と合っているかどうかで、設定（アドレス・decimals・
どちらが原資産か）の正誤を判断できる。** ETH系チェーンは ETH 価格、BSC は BNB 価格が
出るはず。大きくずれる場合（特に 10^12 倍・10^18 倍のようなオーダーのズレ）は
decimals の取り違えを疑い、`config.json` を見直す。

`config.json` の `weth` フィールドは「原資産（WETH/WBNB等）のアドレス」を表し、
`base_is_token0` の判定に使う。プール直接指定のチェーンでは、これに加えて
`base_symbol`（例: `"WBNB"`）が設定されていれば、プールから読み取った実際の
`symbol()` の値ともクロスチェックし、アドレスとシンボルの判定が食い違う場合は
警告を出す（`weth` アドレスの設定ミスに気付くための二重チェック）。

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
| `config.json` | チェーン・トークン・pool/factory の設定、`asset_class`/`pair`、`$10,000`/`±20%` の基準値（`position`） |
| `collect.py` | 収集スクリプト（日次／backfill／`--verify` 対応） |
| `.github/workflows/collect.yml` | 毎日自動実行・自動 commit |
| `index.html` | ダッシュボード（Chart.js のみ・ビルド不要） |
| `data/*.csv` | 蓄積データ（`feeGrowthGlobal` は uint256 を10進文字列として保存） |

### CSV の列

```
date, timestamp, block, pool, pair, asset_class, feeTier,
base_is_token0, dec0, dec1, price, tick, sqrtPriceX96, liquidity,
feeGrowthGlobal0X128, feeGrowthGlobal1X128
```

`base_is_token0` は原資産（ETH/BNB等）が token0 かどうか。`dec0`/`dec1` は
token0/token1 それぞれの decimals（`pair`・チェーンにより 6 でも 18 でもあり得る）。
`price` は原資産1単位あたりの建て通貨（USDC/USDT）建て価格。

ダッシュボード側の集計は「エントリ数」ではなく `timestamp` から算出した実際の経過日数
で正規化している。そのため、RPC失敗等で1日分の行が欠けた場合でも、その前後の
`feeGrowthGlobal` の差分は「実際に経過した日数ぶん」として按分され、年率利回りが
見かけ上インフレすることはない。また `pool` 列が前の行と変わった日（例: factory探索の
結果、手数料ティア移行等で別プールに切り替わった場合）は、その日をまたいだ差分計算を
自動的にスキップする（別コントラクトの累積値を跨いで引き算しないため）。

## 指標の定義

**年率手数料利回り** = 「$10,000 を時価±20%レンジで提供した場合に受け取る手数料」の
30日/90日/全期間合計 ÷ $10,000 × 365/日数

`feeGrowthGlobal`（= 流動性1単位が受け取った手数料の累積値）の日次増分に、その時点の
価格・レンジで $10,000 分の流動性 `L` を掛けて算出する。**プールの TVL や出来高からの
按分ではない** — TVL にはレンジ外で手数料を受け取れない流動性が含まれるため、按分する
と実際の取り分を過大評価する（実測で約1.6倍の乖離を確認済み）。

**取引量（1日あたり）** = `feeGrowthGlobal` の増分 × アクティブ流動性 ÷ 手数料率 で
逆算。Base の実績値（The Graph / Messari）との突合で比率1.087で一致することを
検証済み（この検証は本リポジトリの作成者によるオフチェーンでの事前確認であり、
本リポジトリのコード自体は The Graph / Messari に接続して自動照合する機能を持たない。
数式のみを実装している）。

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
  いない。例えば BNB Smart Chain の USDT は 18桁であり、これをハードコードすると
  価格が桁違いにずれる。`--verify` の価格表示はこの種の設定ミスを見つけるためのもの
  でもある。
- BNB/USDT（BSC）は原資産が ETH ではない参考枠。ETH系チェーンとの直接比較はできない
  （「対象チェーンとペア」参照）。

## 未検証事項・このリポジトリの検証範囲について

**重要: 本リポジトリのコードはオフラインで検証されており、実チェーンへの接続は
未検証。** 実装環境にブロックチェーンRPCへのアウトバウンドネットワークアクセスが
無かったため、`python collect.py --verify` を実際のRPCに対して実行して結果を確認する
ことができなかった。代わりに以下の方法で検証した:

- 実RPCの代わりにモックRPCを使い、`find_pool`/`inspect_pool`/`snapshot`/`block_at`/
  `resolve_base_is_token0`/backfillの重複排除ロジックを、標準的な18/6桁・BSCのような
  18/18桁・BNB/USDTの直接指定シナリオ（`base_is_token0`のアドレス判定とsymbol判定が
  食い違うケースを含む）で検証した。
- ダッシュボードの計算式（`amounts`/`value`/`dailyFees`等）を実際の `data/base.csv`
  （888日分）に対して実行し、有限・非負・妥当な桁感の値が出ることを確認した。
- npm経由でCDNと同一バージョンのChart.js 4.4.1本体（スタブではない）とnode-canvasを
  用意し、実際にレンダリングして888点すべてが可視範囲内に描画されること、
  チェーン/ペア/手数料フィルターが実際に機能することを確認した。
- 複数エージェントによるアドバーサリアルなコードレビューを実施し、見つかった指摘は
  実際にコードを読んで独立に再現・検証した上で反映した（block_atの収束保証、
  RPC失敗時のチェーン単位の分離、日次集計の欠測日耐性、プール切り替え時の
  feeGrowthGlobal誤差分の防止、find_poolのdecimals正規化など）。

**したがって、初回セットアップ時に必ず `python collect.py --verify` を実RPCで
実行し、以下を確認すること:**
1. 全6チェーンについて出力が得られるか（未設定のものは「スキップ」でよい）
2. ETH系チェーンの価格が実勢のETH価格と合っているか
3. **BSC の価格が実勢のBNB価格（数百ドル程度のオーダー）と合っているか** — 大きく
   ずれる場合は `config.json` の `bsc.weth`（WBNBアドレス）が誤っている可能性が高い
4. Unichain の `liquidity` が明らかに0でないか（0の場合「流動性が0」の警告が出る）
5. アドレス判定とsymbol判定の食い違い警告が出ていないか

USDC のネイティブ版／ブリッジ版（USDC.e など）が併存するチェーンについては、
`usdc` を配列にすることで両方を候補として登録でき、`collect.py` が自動的に
流動性の大きい方を選択する（Optimism / Arbitrum は設定済み）。他チェーンで追加の
候補アドレスが必要な場合も同様に配列へ追記すること（本リポジトリでは検証できない
アドレスを推測で追加することはしていない）。

## 制約・既知の注意点

- 秘匿情報（RPC URL・APIキー）はリポジトリに含めない。GitHub Secrets を使う。
- RPC 未設定のチェーンはスキップし、他チェーンの収集は継続する。
- プールが存在しない、または `liquidity` が 0 のチェーンではエラーで落とさず
  「プールが見つかりません」（factory探索時）または「流動性が0」の警告
  （プール直接指定時）として報告し、当該チェーンのみスキップまたは警告付きで継続する。
- `rpc.latest()` 等、日次収集中に想定外のRPCエラーが起きても、その時点までに
  取得できた分は保存した上で該当チェーンだけを中断し、他チェーンの収集は継続する。
- `--backfill` のブロック番号推定は `config.json` の `block_sec` からの概算を
  まず試みるが、実際の平均ブロック時間が設定と大きくズレていても（ハードフォークで
  ブロック時間が変わった場合等）二分探索にフォールバックして必ず正しいブロックに
  収束する。
- ダッシュボードの絞り込みUIはチェーン／ペア／手数料ティアの3軸（既存サイトからの
  引き継ぎ要件）。実際に読み込めたデータに存在する値だけがボタン化される。
