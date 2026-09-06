# -*- coding: utf-8 -*-
"""
Uniswap V3 LP 収益力モニター  ── collect.py
================================================
各チェーンのプールから slot0 / liquidity / feeGrowthGlobal / トークン decimals を取得し、
data/<chain>.csv に追記する。

  日次収集（GitHub Actions が毎日実行）:
      python collect.py

  検証モード（factory/token アドレスと接続を確認）:
      python collect.py --verify

  過去データの一括取得（初回のみ手動）:
      python collect.py --backfill 400            # 全チェーン、直近400日分
      python collect.py --backfill 30 base         # base のみ、直近30日分

RPC URL は環境変数から読む（config.json の rpc_env 参照）。
未設定のチェーンは自動でスキップし、他のチェーンの処理は継続する。

注意:
  - RPC はアーカイブノードが必要（過去ブロックの eth_call を行うため）。
    公式の無料エンドポイント（例: https://mainnet.base.org）は直近の状態しか
    保持しておらず backfill には使えない。
  - feeGrowthGlobal は uint256 だが、Python の int は多倍長なので桁落ちしない。
    CSV には10進文字列として保存し、JavaScript 側では BigInt で読むこと。
"""
import csv, json, math, os, sys, time, urllib.request
from datetime import datetime, timezone, timedelta

CFG = json.load(open("config.json", encoding="utf-8"))
Q96, Q128 = 2 ** 96, 2 ** 128
SEL = {
    "getPool": "0x1698ee82",
    "slot0": "0x3850c7bd",
    "liquidity": "0x1a686502",
    "fg0": "0xf3058399",
    "fg1": "0x46141319",
    "token0": "0x0dfe1681",
    "decimals": "0x313ce567",
}
FEE_TIERS = (100, 500, 3000, 10000)


class RPC:
    def __init__(self, url):
        self.url, self.n, self.cache = url, 0, {}

    def call(self, method, params):
        self.n += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params}).encode()
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
        for a in range(5):
            try:
                with urllib.request.urlopen(req, timeout=45) as r:
                    o = json.loads(r.read())
                if "error" in o:
                    raise RuntimeError(o["error"])
                return o["result"]
            except Exception as e:
                if a == 4:
                    raise
                time.sleep(1.2 * (a + 1))

    def eth_call(self, to, data, blk="latest"):
        tag = hex(blk) if isinstance(blk, int) else blk
        return self.call("eth_call", [{"to": to, "data": data}, tag])

    def block_ts(self, n):
        if n not in self.cache:
            self.cache[n] = int(self.call("eth_getBlockByNumber", [hex(n), False])["timestamp"], 16)
        return self.cache[n]

    def latest(self):
        return int(self.call("eth_blockNumber", []), 16)


def enc_a(a): return a.lower().replace("0x", "").rjust(64, "0")
def enc_u(n): return hex(n)[2:].rjust(64, "0")
def s256(h):
    v = int(h, 16)
    return v - 2 ** 256 if v >= 2 ** 255 else v


def read_decimals(rpc, token):
    return int(rpc.eth_call(token, SEL["decimals"]), 16)


def find_pool(rpc, c, verbose=False):
    """全手数料ティア × USDC候補を調べ、流動性が最大のプールを返す"""
    best = None
    usdcs = c["usdc"] if isinstance(c["usdc"], list) else [c["usdc"]]
    for usdc in usdcs:
      for fee in FEE_TIERS:
        addr = "0x" + rpc.eth_call(c["factory"], SEL["getPool"] + enc_a(c["weth"]) + enc_a(usdc) + enc_u(fee))[-40:]
        if int(addr, 16) == 0:
            if verbose: print(f"    {fee/10000:>5.2f}%  usdc={usdc[:10]}…  プールなし")
            continue
        try:
            liq = int(rpc.eth_call(addr, SEL["liquidity"]), 16)
        except Exception as e:
            if verbose: print(f"    {fee/10000:>5.2f}%  {addr}  読取失敗 {e}")
            continue
        if verbose: print(f"    {fee/10000:>5.2f}%  {addr}  liquidity={liq:.3e}")
        if best is None or liq > best[0]:
            best = (liq, fee, addr, usdc)
    if not best or best[0] == 0:
        raise RuntimeError("プールが見つかりません（factory/token アドレスを確認してください）")
    liq, fee, addr, usdc = best
    c["usdc"] = usdc
    t0 = "0x" + rpc.eth_call(addr, SEL["token0"])[-40:]
    weth_is_token0 = t0.lower() == c["weth"].lower()
    weth_dec = read_decimals(rpc, c["weth"])
    usdc_dec = read_decimals(rpc, usdc)
    return addr, fee, weth_is_token0, weth_dec, usdc_dec


def snapshot(rpc, pool, weth_is_token0, weth_dec, usdc_dec, blk):
    raw = rpc.eth_call(pool, SEL["slot0"], blk)[2:]
    sq = int(raw[0:64], 16)
    tick = s256(raw[64:128])
    # sqrtPriceX96 は token1/token0 の「生の（decimals未調整）」価格の平方根。
    # 人間可読の価格 = (sqrtP/2^96)^2 × 10^(token0decimals - token1decimals)。
    # token0=WETH ならこれがそのまま ETH の USD 価格、token1=WETH なら逆数を取る。
    # (WETH/USDC が常に 18/6 桁とは限らない。例: BNB Smart Chain の USDC は 18桁)
    t0dec, t1dec = (weth_dec, usdc_dec) if weth_is_token0 else (usdc_dec, weth_dec)
    p_raw = (sq / Q96) ** 2
    p_human = p_raw * (10 ** (t0dec - t1dec))
    px = p_human if weth_is_token0 else (1 / p_human if p_human else 0.0)
    return {
        "block": blk,
        "eth_usd": round(px, 4), "tick": tick, "sqrtPriceX96": sq,
        "liquidity": int(rpc.eth_call(pool, SEL["liquidity"], blk), 16),
        "feeGrowthGlobal0X128": int(rpc.eth_call(pool, SEL["fg0"], blk), 16),
        "feeGrowthGlobal1X128": int(rpc.eth_call(pool, SEL["fg1"], blk), 16),
    }


def block_at(rpc, ts, lo, hi, block_sec):
    """二分探索ではなく、ブロック間隔から標的を推定して数回で収束させる"""
    b = min(max(lo, 1), hi)
    for _ in range(16):
        d = ts - rpc.block_ts(b)
        if abs(d) <= 4:
            break
        nb = min(max(b + int(d / block_sec), 1), hi)
        if nb == b:
            break
        b = nb
    for _ in range(100):
        if b > 1 and rpc.block_ts(b - 1) >= ts:
            b -= 1
        elif b < hi and rpc.block_ts(b) < ts:
            b += 1
        else:
            break
    return b


COLS = ["date", "timestamp", "block", "pool", "feeTier", "weth_is_token0",
        "wethDecimals", "usdcDecimals", "eth_usd", "tick",
        "sqrtPriceX96", "liquidity", "feeGrowthGlobal0X128", "feeGrowthGlobal1X128"]


def load_csv(path):
    if not os.path.exists(path):
        return []
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    # 旧スキーマ（decimals列が無い）のCSVとの後方互換: WETH=18桁, USDC=6桁 と仮定する。
    for r in rows:
        r.setdefault("wethDecimals", "18")
        r.setdefault("usdcDecimals", "6")
    return rows


def save_csv(path, rows):
    rows = sorted({r["date"]: r for r in rows}.values(), key=lambda r: r["date"])
    # lineterminator="\n": Python の csv モジュールは既定で "\r\n" を書き出すが、
    # ブラウザ側で単純に split("\n") すると各行末に "\r" が残り最終列
    # (feeGrowthGlobal1X128) の値が壊れる。明示的に "\n" 終端にする。
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def run_chain(name, c, backfill_days=0):
    url = os.environ.get(c["rpc_env"], "")
    if not url:
        print(f"[{name}] 環境変数 {c['rpc_env']} が未設定。スキップ")
        return
    rpc = RPC(url)
    try:
        pool, fee, w0, weth_dec, usdc_dec = find_pool(rpc, c)
    except Exception as e:
        print(f"[{name}] プール検出失敗: {e}")
        return
    print(f"[{name}] pool={pool} fee={fee/10000:.2f}% weth_is_token0={w0} "
          f"wethDecimals={weth_dec} usdcDecimals={usdc_dec}")

    path = f"data/{name}.csv"
    rows = load_csv(path)
    have = {r["date"] for r in rows}
    latest = rpc.latest()
    today = datetime.now(timezone.utc).date()

    if backfill_days:
        targets = [today - timedelta(days=i) for i in range(backfill_days, 0, -1)]
    else:
        targets = [today]

    cur = max(1, latest - int(backfill_days * 86400 / c["block_sec"]) - 1000) if backfill_days else latest
    added = 0
    for d in targets:
        ds = d.isoformat()
        if ds in have:
            continue
        ts = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
        try:
            blk = latest if not backfill_days else block_at(rpc, ts, cur, latest, c["block_sec"])
            cur = blk
            s = snapshot(rpc, pool, w0, weth_dec, usdc_dec, blk)
            rows.append({"date": ds, "timestamp": ts, "pool": pool, "feeTier": fee,
                         "weth_is_token0": w0, "wethDecimals": weth_dec, "usdcDecimals": usdc_dec, **s})
            added += 1
            if added % 30 == 0:
                print(f"  {ds}  ETH ${s['eth_usd']:,.2f}  liq={s['liquidity']:.3e}")
        except Exception as e:
            print(f"  ! {ds}: {e}")
    n = save_csv(path, rows)
    print(f"[{name}] +{added}件 → 計{n}件  (RPC {rpc.n}回)")


def verify():
    """各チェーンの factory / token アドレスが正しいかを実際に問い合わせて確認する"""
    print("=" * 78)
    print("アドレス検証  （python collect.py --verify）")
    print("=" * 78)
    for name, c in CFG["chains"].items():
        url = os.environ.get(c["rpc_env"], "")
        print(f"\n■ {name}")
        if not url:
            print(f"  環境変数 {c['rpc_env']} が未設定 → スキップ")
            continue
        rpc = RPC(url)
        try:
            cid = int(rpc.call("eth_chainId", []), 16)
            blk = rpc.latest()
            print(f"  接続OK  chainId={cid}  最新ブロック={blk:,}")
        except Exception as e:
            print(f"  接続失敗: {e}")
            continue
        print(f"  factory={c['factory']}")
        try:
            pool, fee, w0, weth_dec, usdc_dec = find_pool(rpc, c, verbose=True)
            print(f"  → 採用: {pool}  手数料{fee/10000:.2f}%  WETHはtoken{'0' if w0 else '1'}"
                  f"  (wethDecimals={weth_dec}, usdcDecimals={usdc_dec})")
            s = snapshot(rpc, pool, w0, weth_dec, usdc_dec, "latest")
            print(f"  → ETH価格 ${s['eth_usd']:,.2f}  ← 実勢と合っていれば設定は正しい")
        except Exception as e:
            print(f"  × {e}")
    print("\n" + "=" * 78)


if __name__ == "__main__":
    if "--verify" in sys.argv:
        verify(); sys.exit(0)
    bf = 0
    if "--backfill" in sys.argv:
        bf = int(sys.argv[sys.argv.index("--backfill") + 1])
    only = [a for a in sys.argv[1:] if a in CFG["chains"]]
    for name, c in CFG["chains"].items():
        if only and name not in only:
            continue
        run_chain(name, c, bf)
