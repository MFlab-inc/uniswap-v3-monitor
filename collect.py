# -*- coding: utf-8 -*-
"""
Uniswap V3 LP 収益力モニター  ── collect.py
================================================
各チェーンのプールから slot0 / liquidity / feeGrowthGlobal / トークン decimals・symbol を
取得し、data/<chain>.csv に追記する。

  日次収集（GitHub Actions が毎日実行）:
      python collect.py

  検証モード（factory/token アドレスと接続を確認）:
      python collect.py --verify

  過去データの一括取得（初回のみ手動）:
      python collect.py --backfill 400            # 全チェーン、直近400日分
      python collect.py --backfill 30 base         # base のみ、直近30日分

RPC URL は環境変数から読む（config.json の rpc_env 参照）。
未設定のチェーンは自動でスキップし、他のチェーンの処理は継続する。

プールの決め方は2通り:
  - config.json に "pool" が無いチェーン: factory から weth/usdc の全手数料ティアを
    探索し、流動性最大のプールを自動選択する（find_pool）。
  - "pool" が指定されているチェーン（bsc, unichain）: そのプールを直接使い、
    token0/token1/fee/decimals/symbol をプール自身に問い合わせて構成を判定する
    （inspect_pool）。COINPOOL が実際に運用するプールが ETH/USDC とは限らない
    （例: BNB Smart Chain は BNB/USDT）ため、factory 探索をスキップする。

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
    "token1": "0xd21220a7",
    "fee": "0xddca3f43",
    "decimals": "0x313ce567",
    "symbol": "0x95d89b41",
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


def token_meta(rpc, addr):
    """トークンの小数桁とシンボルを取得する。symbol() は string(動的) と bytes32(旧規格) の
    両方の ABI エンコーディングがあり得るため両対応する。取得できなければ "?" とする。"""
    dec = int(rpc.eth_call(addr, SEL["decimals"]), 16)
    try:
        raw = rpc.eth_call(addr, SEL["symbol"])[2:]
        if len(raw) > 128:                       # 動的 string（offset 32byte + length 32byte + data）
            ln = int(raw[64:128], 16)
            sym = bytes.fromhex(raw[128:128 + ln * 2]).decode("utf-8", "replace")
        else:                                    # bytes32 固定長（例: 旧式トークン）
            sym = bytes.fromhex(raw).rstrip(b"\x00").decode("utf-8", "replace")
    except Exception:
        sym = "?"
    return dec, sym


def inspect_pool(rpc, addr):
    """プールから token0/token1・小数桁・symbol・手数料ティア・現在の流動性を読み取る"""
    t0 = "0x" + rpc.eth_call(addr, SEL["token0"])[-40:]
    t1 = "0x" + rpc.eth_call(addr, SEL["token1"])[-40:]
    fee = int(rpc.eth_call(addr, SEL["fee"]), 16)
    d0, s0 = token_meta(rpc, t0)
    d1, s1 = token_meta(rpc, t1)
    liq = int(rpc.eth_call(addr, SEL["liquidity"]), 16)
    return {"pool": addr, "token0": t0, "token1": t1, "fee": fee,
            "d0": d0, "d1": d1, "sym0": s0, "sym1": s1, "liquidity": liq}


def find_pool(rpc, c, verbose=False):
    """全手数料ティア × USDC候補を調べ、流動性が最大のプールを返す。

    liquidity() の生の値はトークンの decimals に依存するため、USDC候補間で
    decimals が異なる場合（例: 6桁 vs 18桁）は生の整数のまま比較すると経済的な
    深さの比較にならない。候補ごとに quote 側の decimals を取得し、
    10**(decimals/2) で正規化してから比較する（liquidity ~ sqrt(x*y) であり、
    quote側のraw量は10**decimalsに比例するため）。
    """
    best = None
    usdcs = c["usdc"] if isinstance(c["usdc"], list) else [c["usdc"]]
    usdc_dec_cache = {}
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
        if usdc not in usdc_dec_cache:
            try:
                usdc_dec_cache[usdc] = int(rpc.eth_call(usdc, SEL["decimals"]), 16)
            except Exception:
                usdc_dec_cache[usdc] = 6  # 取得失敗時は標準的な6桁を仮定(比較用の参考値)
        norm_liq = liq / (10 ** (usdc_dec_cache[usdc] / 2))
        if verbose: print(f"    {fee/10000:>5.2f}%  {addr}  liquidity={liq:.3e} (正規化後={norm_liq:.3e})")
        if best is None or norm_liq > best[0]:
            best = (norm_liq, fee, addr, usdc)
    if not best or best[0] == 0:
        raise RuntimeError("プールが見つかりません（factory/token アドレスを確認してください）")
    _, fee, addr, usdc = best
    c["usdc"] = usdc
    t0 = "0x" + rpc.eth_call(addr, SEL["token0"])[-40:]
    base_is_token0 = t0.lower() == c["weth"].lower()
    return addr, fee, base_is_token0


def resolve_base_is_token0(c, m):
    """token0 が「原資産」(WETH/WBNB 等) かどうかを判定する。

    まず config.json の "weth" アドレスとの一致で判定し、"base_symbol" が設定されて
    いれば symbol() の値でも独立にクロスチェックする。両者が食い違う場合は設定ミスの
    可能性が高いため警告する（例: このチェーンの原資産アドレスの設定が古い/誤っている）。
    """
    by_addr = None
    if c.get("weth"):
        by_addr = m["token0"].lower() == c["weth"].lower()
    by_symbol = None
    hint = c.get("base_symbol")
    if hint:
        s0, s1 = m["sym0"].upper(), m["sym1"].upper()
        if s0 == hint.upper():
            by_symbol = True
        elif s1 == hint.upper():
            by_symbol = False
    warn = None
    if by_addr is not None and by_symbol is not None and by_addr != by_symbol:
        warn = (f"⚠ アドレス判定(base_is_token0={by_addr})とsymbol判定(base_is_token0={by_symbol})が食い違う。"
                f"token0={m['sym0']} token1={m['sym1']} — config.json の weth/base_symbol を確認すること")
    result = by_symbol if by_symbol is not None else by_addr
    if result is None:
        warn = warn or "⚠ 原資産(token0/token1のどちらか)を判定できません。config.json に weth か base_symbol を設定してください"
        result = True  # 便宜上の既定値。warn を必ず表示するので気付ける。
    return result, warn


def snapshot(rpc, pool, d0, d1, base_is_token0, blk):
    """base_is_token0: 原資産(ETH/BNB等)が token0 か。price は「原資産1単位あたりの建て通貨量」"""
    raw = rpc.eth_call(pool, SEL["slot0"], blk)[2:]
    sq = int(raw[0:64], 16)
    tick = s256(raw[64:128])
    # sqrtPriceX96 は token1/token0 の「生の（decimals未調整）」価格の平方根。
    # 人間可読の価格 = (sqrtP/2^96)^2 × 10^(dec0-dec1)。token0が原資産ならそのまま、
    # token1が原資産なら逆数を取る。(原資産/建て通貨が常に18/6桁とは限らない。
    # 例: BNB Smart Chain の USDC は18桁)
    p_raw = (sq / Q96) ** 2
    p = p_raw * (10 ** (d0 - d1))
    px = p if base_is_token0 else (1 / p if p else 0.0)
    return {
        "block": blk,
        "price": round(px, 6), "tick": tick, "sqrtPriceX96": sq,
        "liquidity": int(rpc.eth_call(pool, SEL["liquidity"], blk), 16),
        "feeGrowthGlobal0X128": int(rpc.eth_call(pool, SEL["fg0"], blk), 16),
        "feeGrowthGlobal1X128": int(rpc.eth_call(pool, SEL["fg1"], blk), 16),
    }


def block_at(rpc, ts, lo, hi, block_sec):
    """タイムスタンプ ts に対応するブロック番号を返す。

    まず config.json の block_sec からの概算で数回のうちに収束させる（高速パス。
    Base で2,184点を111秒で取得できたのはこの経路による）。ただし block_sec の
    設定が実際の平均ブロック時間と大きくズレている場合（ハードフォークでブロック時間
    が変わった、設定ミス等）、固定レートの外挿だけでは収束せず同じ2状態を往復し
    続けることがある。そこで概算が収束しなかった場合は、ブロック番号に対して
    タイムスタンプが単調増加であることを利用した二分探索にフォールバックし、
    block_sec の精度に関係なく必ず正しいブロックに収束させる。
    """
    lo, hi = max(1, lo), max(1, hi)
    b = min(max(lo, 1), hi)
    for _ in range(16):
        d = ts - rpc.block_ts(b)
        if abs(d) <= 1:
            return b
        nb = min(max(b + int(d / block_sec), 1), hi)
        if nb == b:
            break
        b = nb

    # --- 二分探索フォールバック ---
    # 事前条件: block_ts(lo) <= ts <= block_ts(hi) を保証する。
    if rpc.block_ts(hi) <= ts:
        return hi
    guard = 0
    while lo > 1 and rpc.block_ts(lo) > ts and guard < 64:
        lo = max(1, lo - (hi - lo) * 2 - 1)  # lo側が足りなければ指数的に広げる
        guard += 1
    lo2, hi2 = lo, hi
    while hi2 - lo2 > 1:
        mid = (lo2 + hi2) // 2
        if rpc.block_ts(mid) <= ts:
            lo2 = mid
        else:
            hi2 = mid
    return lo2


COLS = ["date", "timestamp", "block", "pool", "pair", "asset_class", "feeTier",
        "base_is_token0", "dec0", "dec1", "price", "tick",
        "sqrtPriceX96", "liquidity", "feeGrowthGlobal0X128", "feeGrowthGlobal1X128"]


def load_csv(path):
    if not os.path.exists(path):
        return []
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    # 後方互換のための既定値（decimals/pair/asset_class列の無い旧スキーマのCSV対策）
    for r in rows:
        r.setdefault("dec0", "18")
        r.setdefault("dec1", "6")
        r.setdefault("pair", "?")
        r.setdefault("asset_class", "eth")
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


def locate(rpc, name, c, verbose=False):
    """設定からプール構成を決定する。"pool" が指定されていれば直接使い（factory 探索なし）、
    無ければ factory から探索する。戻り値: (pool, fee, base_is_token0, d0, d1, warn|None)"""
    if c.get("pool"):
        m = inspect_pool(rpc, c["pool"])
        base0, warn = resolve_base_is_token0(c, m)
        if verbose:
            print(f"    プール直接指定: {c['pool']}")
            print(f"    token0={m['sym0']}({m['d0']}桁) {m['token0']}")
            print(f"    token1={m['sym1']}({m['d1']}桁) {m['token1']}")
            print(f"    手数料{m['fee']/10000:.2f}%  liquidity={m['liquidity']:.3e}")
        if m["liquidity"] == 0:
            w2 = "⚠ 流動性が0。運用対象になっていない可能性がある"
            warn = (warn + " / " + w2) if warn else w2
        return m["pool"], m["fee"], base0, m["d0"], m["d1"], warn
    else:
        pool, fee, base0 = find_pool(rpc, c, verbose=verbose)
        m = inspect_pool(rpc, pool)
        warn = None
        if m["liquidity"] == 0:
            warn = "⚠ 流動性が0。運用対象になっていない可能性がある"
        if verbose:
            print(f"    → 採用: {pool}  {m['sym0']}/{m['sym1']}  手数料{fee/10000:.2f}%")
        return pool, fee, base0, m["d0"], m["d1"], warn


def run_chain(name, c, backfill_days=0):
    url = os.environ.get(c["rpc_env"], "")
    if not url:
        print(f"[{name}] 環境変数 {c['rpc_env']} が未設定。スキップ")
        return
    rpc = RPC(url)
    try:
        pool, fee, base0, d0, d1, warn = locate(rpc, name, c)
    except Exception as e:
        print(f"[{name}] プール検出失敗: {e}")
        return
    print(f"[{name}] pool={pool} pair={c.get('pair','?')} fee={fee/10000:.2f}% base_is_token0={base0} "
          f"dec0={d0} dec1={d1}")
    if warn:
        print(f"  {warn}")

    path = f"data/{name}.csv"
    rows = load_csv(path)
    have = {r["date"] for r in rows}

    # プールが前回と変わっていないか確認する。factory探索は毎回「現時点で最も流動性が
    # 大きいプール」を選び直すため、手数料ティア移行等でプールが変わることがあり得る。
    # feeGrowthGlobalは別コントラクトの値を跨いで差分を取ってはならないため、
    # 気付けるよう警告のみ出す（実際のスキップ処理はダッシュボード側のpool列比較で行う）。
    if rows:
        prev_pool = rows[-1].get("pool", "")
        if prev_pool and prev_pool.lower() != pool.lower():
            print(f"  ⚠ プールが変わりました（前回={prev_pool} → 今回={pool}）。"
                  f"この日を境に feeGrowthGlobal の差分計算はダッシュボード側でスキップされる")

    # ここから先は個別チェーンの一時的なRPC不調（eth_blockNumber等）で例外が出ても、
    # 他のチェーンの処理を止めないようにする。finally で必ず save_csv を呼ぶことで、
    # 途中で中断しても、その時点までに取得できた分は失われない。
    added = 0
    try:
        latest = rpc.latest()
        today = datetime.now(timezone.utc).date()

        if backfill_days:
            targets = [today - timedelta(days=i) for i in range(backfill_days, 0, -1)]
        else:
            targets = [today]

        cur = max(1, latest - int(backfill_days * 86400 / c["block_sec"]) - 1000) if backfill_days else latest
        for d in targets:
            ds = d.isoformat()
            if ds in have:
                continue
            ts = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
            try:
                blk = latest if not backfill_days else block_at(rpc, ts, cur, latest, c["block_sec"])
                cur = blk
                s = snapshot(rpc, pool, d0, d1, base0, blk)
                rows.append({"date": ds, "timestamp": ts, "pool": pool, "pair": c.get("pair", "?"),
                             "asset_class": c.get("asset_class", "eth"), "feeTier": fee,
                             "base_is_token0": base0, "dec0": d0, "dec1": d1, **s})
                added += 1
                if added % 30 == 0:
                    print(f"  {ds}  価格 {s['price']:,.4f}  liq={s['liquidity']:.3e}")
            except Exception as e:
                print(f"  ! {ds}: {e}")
    except Exception as e:
        print(f"[{name}] 収集を中断しました（{e}）。取得済み分のみ保存します")
    finally:
        n = save_csv(path, rows)
        print(f"[{name}] +{added}件 → 計{n}件  (RPC {rpc.n}回)")


def verify():
    """各チェーンの factory/pool・token アドレスが正しいかを実際に問い合わせて確認する"""
    print("=" * 78)
    print("アドレス検証  （python collect.py --verify）")
    print("=" * 78)
    for name, c in CFG["chains"].items():
        url = os.environ.get(c["rpc_env"], "")
        print(f"\n■ {name}  ({c.get('pair', '?')}"
              f"{'・参考枠(原資産≠ETH)' if c.get('asset_class') == 'other' else ''})")
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
        if not c.get("pool"):
            print(f"  factory={c['factory']}")
        try:
            pool, fee, base0, d0, d1, warn = locate(rpc, name, c, verbose=True)
            if warn:
                print(f"    {warn}")
            s = snapshot(rpc, pool, d0, d1, base0, "latest")
            print(f"  → 価格 {s['price']:,.4f}  ({c.get('pair', '?')})  ← 実勢と合っていれば設定は正しい")
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
