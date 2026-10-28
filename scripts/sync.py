#!/usr/bin/env python3
"""
선물 포지션 히스토리 → data/trades.json 동기화 (OKX, Bitget)
- 외부 패키지 없이 표준 라이브러리만 사용 (GitHub Actions에서 바로 실행)
- API 키가 등록된 거래소만 수집. 한 거래소가 실패해도 나머지는 계속 진행
- 첫 실행: 거래소가 보관하는 최근 3개월치 백필 → 이후 변경분만 받아 저장소에 영구 누적
- 하루 1회 거래소별 선물 계좌 자산 스냅샷 → 수익 캘린더의 일별 % 계산용
- 거래소별 수집 결과는 data/status.json 에 기록 (사이트 상단에 표시)

GitHub Secrets (모두 "읽기 전용" 키, 사이트 설정 화면에서 등록):
  OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE
  BITGET_API_KEY, BITGET_SECRET_KEY, BITGET_PASSPHRASE
  워크플로는 모든 Secrets를 TJ_SECRETS(JSON)로 넘겨주므로, 거래소를 추가해도 워크플로 파일은 바꿀 필요가 없음
선택 환경변수:
  OKX_BASE_URL          기본 https://openapi.okx.com (실패 시 www.okx.com 재시도)
  OKX_INST_TYPES        기본 SWAP           (만기선물 포함: "SWAP,FUTURES")
  BITGET_PRODUCT_TYPES  기본 USDT-FUTURES   (USDC 포함: "USDT-FUTURES,USDC-FUTURES")
"""
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TRADES_FILE = DATA / "trades.json"
EQUITY_FILE = DATA / "equity.json"
NOTES_FILE = DATA / "notes.json"
STATUS_FILE = DATA / "status.json"
REBATES_FILE = DATA / "rebates.json"
FX_FILE = DATA / "fx.json"
BALANCE_FILE = DATA / "balance.json"  # 지금 계좌 잔고 (수집 때마다)
OPEN_FILE = DATA / "open.json"   # 보유 중 포지션의 누적 펀딩비 (청산되면 체결 기록에 합쳐짐)

KST = dt.timezone(dt.timedelta(hours=9))
UA = "trade-journal-sync/2.0"
# 증분 동기화 시 마지막 업데이트보다 이만큼 과거까지 다시 훑음 (부분청산 → 전체청산 갱신 대비)
OVERLAP_MS = 3 * 24 * 60 * 60 * 1000


class ApiError(RuntimeError):
    pass


VERSION = "3.17"
try:
    _SECRETS = json.loads(os.environ.get("TJ_SECRETS") or "{}")
except ValueError:
    _SECRETS = {}


def env(name: str, default: str = "") -> str:
    """환경변수 → TJ_SECRETS(JSON) → 기본값 순으로 찾음"""
    v = os.environ.get(name)
    if v is None or v == "":
        v = _SECRETS.get(name, default)
    return str(v or "").strip()


def num(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def http_get_json(url: str, headers: dict, timeout=20):
    """(status, json|None). 네트워크 오류는 예외."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"_raw": raw[:200]}


def blocked_hint(status: int) -> str:
    if status in (403, 451):
        return " — 서버 위치(미국 IP) 차단일 가능성이 높아요. README의 '거래소가 차단할 때' 참고"
    return ""


# ═════════════════════════════ OKX ═════════════════════════════
class OKX:
    name = "okx"
    CLOSED = {"2", "3", "6"}  # 전체청산, 강제청산, ADL 전체청산

    def __init__(self):
        self.key, self.secret, self.pp = env("OKX_API_KEY"), env("OKX_SECRET_KEY"), env("OKX_PASSPHRASE")
        self.bases = [env("OKX_BASE_URL").rstrip("/") or "https://openapi.okx.com", "https://www.okx.com"]
        self.inst_types = [s.strip() for s in env("OKX_INST_TYPES", "SWAP").split(",") if s.strip()]
        self._base = None

    @staticmethod
    def enabled():
        return bool(env("OKX_API_KEY"))

    def get(self, path, params=None, private=True):
        q = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v not in (None, "")})
        req_path = path + ("?" + q if q else "")
        last = None
        for base in ([self._base] if self._base else self.bases):
            for attempt in range(5):
                h = {"Content-Type": "application/json"}
                if private:
                    now = dt.datetime.now(dt.timezone.utc)
                    ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
                    sign = base64.b64encode(hmac.new(self.secret.encode(), (ts + "GET" + req_path).encode(), hashlib.sha256).digest()).decode()
                    h.update({"OK-ACCESS-KEY": self.key, "OK-ACCESS-SIGN": sign, "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": self.pp})
                try:
                    status, body = http_get_json(base + req_path, h)
                except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                    last = ApiError(f"OKX 연결 실패: {e}")
                    break
                code = str(body.get("code", ""))
                if code == "0":
                    self._base = base
                    return body.get("data", [])
                if code == "50011" or status == 429:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                if "_raw" in body:
                    last = ApiError(f"OKX HTTP {status}{blocked_hint(status)}")
                    break
                raise ApiError(f"OKX 오류 code={code} {body.get('msg', '')}")
        raise last or ApiError("OKX 알 수 없는 오류")

    def ct_vals(self):
        out = {}
        for it in self.inst_types:
            try:
                for ins in self.get("/api/v5/public/instruments", {"instType": it}, private=False):
                    v = num(ins.get("ctVal"))
                    if v:
                        out[ins["instId"]] = v
            except ApiError as e:
                print(f"  [경고] OKX 계약 정보 조회 실패({it}): {e}")
        return out

    def fetch(self, stop_ms):
        ct = self.ct_vals()
        out = []
        for it in self.inst_types:
            after, pages = None, 0
            while pages < 200:
                page = self.get("/api/v5/account/positions-history", {"instType": it, "limit": "100", "after": after})
                pages += 1
                if not page:
                    break
                out.extend(self.normalize(r, ct) for r in page)
                oldest = min(int(r["uTime"]) for r in page)
                if (stop_ms and oldest < stop_ms) or len(page) < 100 or str(oldest) == after:
                    break
                after = str(oldest)
                time.sleep(0.25)
        return out

    def normalize(self, r, ct):
        t = {
            "id": f"{r['posId']}_{r['cTime']}",  # 기존 데이터와 호환되도록 OKX는 접두어 없음
            "exchange": "okx",
            "posId": r["posId"],
            "instId": r.get("instId", ""),
            "base": r.get("instId", "").split("-")[0],
            "mgnMode": r.get("mgnMode", ""),
            "direction": r.get("direction") or r.get("posSide", ""),
            "ccy": r.get("ccy", ""),
            "type": str(r.get("type", "")),
            "cTime": int(r["cTime"]),
            "uTime": int(r["uTime"]),
            "lever": num(r.get("lever")),
            "openAvgPx": num(r.get("openAvgPx")),
            "closeAvgPx": num(r.get("closeAvgPx")),
            "openMaxPos": num(r.get("openMaxPos")),
            "closeTotalPos": num(r.get("closeTotalPos")),
            "posUnit": "contract",
            "pnl": num(r.get("pnl")),
            "fee": num(r.get("fee")),
            "fundingFee": num(r.get("fundingFee")),
            "liqPenalty": num(r.get("liqPenalty")),
            "realizedPnl": num(r.get("realizedPnl")),
            "pnlRatio": num(r.get("pnlRatio")),
        }
        t["status"] = "closed" if t["type"] in self.CLOSED else "partial"
        cv = ct.get(t["instId"])
        if cv and t["openMaxPos"] and t["openAvgPx"]:
            t["notional"] = round(t["openMaxPos"] * cv * (t["openAvgPx"] if t["ccy"] in ("USDT", "USDC", "USD") else 1), 4)
            if t["lever"]:
                t["margin"] = round(t["notional"] / t["lever"], 4)
        return t

    def equity(self):
        bal = self.get("/api/v5/account/balance")
        return num(bal[0].get("totalEq")) if bal else None

    def balance(self):
        """거래 계좌 잔고 + (가능하면) 계정 전체 평가액."""
        b = (self.get("/api/v5/account/balance") or [{}])[0]
        r2 = lambda v: round(v, 2) if v is not None else None
        usdt = next((d for d in b.get("details", []) if d.get("ccy") == "USDT"), {})
        out = {"equity": r2(num(b.get("totalEq"))), "avail": r2(num(usdt.get("availBal")) if usdt else num(b.get("availEq"))),
               "margin": r2(num(b.get("imr")) or num(usdt.get("imr")) or num(usdt.get("frozenBal"))), "upl": r2(num(b.get("upl")) if b.get("upl") not in (None, "") else sum(num(d.get("upl")) or 0 for d in b.get("details", [])))}
        try:  # 자금·적립 계좌까지 합친 평가액 (USDT 기준)
            v = (self.get("/api/v5/asset/asset-valuation", {"ccy": "USDT"}) or [{}])[0]
            det = v.get("details") or {}
            out["total"] = r2(num(v.get("totalBal")))
            out["funding"] = r2(num(det.get("funding")))
            out["earn"] = r2(num(det.get("earn")))
        except ApiError as e:
            print(f"  [안내] 전체 평가액 조회 실패: {e}")
        return out

    def open_positions(self):
        """보유 중 포지션. fundingFee = 포지션을 연 뒤 지금까지 누적 펀딩비 (+받음 / -냄)."""
        out, ct = [], None
        for r in self.get("/api/v5/account/positions") or []:
            pos = num(r.get("pos")) or 0
            if not pos or r.get("instType") not in ("SWAP", "FUTURES"):
                continue
            if ct is None:
                ct = self.ct_vals()
            side = r.get("posSide")
            direction = side if side in ("long", "short") else ("long" if pos > 0 else "short")
            inst = r.get("instId", "")
            cv = ct.get(inst) or 1.0
            usd = r.get("ccy", "USDT") in ("USDT", "USDC", "USD")
            out.append({"id": f"{r.get('posId')}_{r.get('cTime')}", "instId": inst, "base": inst.split("-")[0],
                        "direction": direction, "lever": num(r.get("lever")), "mgnMode": r.get("mgnMode"), "cTime": int(r.get("cTime") or 0),
                        "qty": round(abs(pos) * cv, 8) if usd else None, "contracts": abs(pos), "avgPx": num(r.get("avgPx")),
                        "markPx": num(r.get("markPx")), "liqPx": num(r.get("liqPx")), "upl": round(num(r.get("upl")) or 0.0, 4),
                        "uplRatio": num(r.get("uplRatio")), "margin": num(r.get("margin")) or num(r.get("imr")),
                        "notional": round(abs(num(r.get("notionalUsd")) or 0.0), 2), "ccy": r.get("ccy", "USDT"),
                        "fundingFee": round(num(r.get("fundingFee")) or 0.0, 6), "fee": round(num(r.get("fee")) or 0.0, 6)})
        return out

    def rebates(self, since_ms):
        """수수료 페이백 입금 내역 (자금 계좌 내역 type 173, 메모 'Fee rebate'). since_ms 이후만."""
        out, after, pages = [], None, 0
        while pages < 100:
            rows = self.get("/api/v5/asset/bills", {"type": "173", "limit": "100", "after": after})
            pages += 1
            if not rows:
                break
            for r in rows:
                ts = int(r.get("ts") or 0)
                if since_ms and ts < since_ms:
                    continue
                out.append({"id": r.get("billId"), "ts": ts, "amt": num(r.get("balChg")) or 0.0, "ccy": r.get("ccy", "")})
            oldest = min(int(r.get("ts") or 0) for r in rows)
            if (since_ms and oldest < since_ms) or len(rows) < 100:
                break
            after = str(oldest)
            time.sleep(0.25)
        return out


# ═════════════════════════════ Bitget (클래식 계정, API v2) ═════════════════════════════
class Bitget:
    name = "bitget"

    def __init__(self):
        self.key, self.secret, self.pp = env("BITGET_API_KEY"), env("BITGET_SECRET_KEY"), env("BITGET_PASSPHRASE")
        self.products = [s.strip() for s in env("BITGET_PRODUCT_TYPES", "USDT-FUTURES").split(",") if s.strip()]
        self._lev_cache = {}
        self.BASE = env("BITGET_BASE_URL").rstrip("/") or "https://api.bitget.com"

    @staticmethod
    def enabled():
        return bool(env("BITGET_API_KEY"))

    def get(self, path, params=None):
        q = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v not in (None, "")})
        req_path = path + ("?" + q if q else "")
        for attempt in range(5):
            ts = str(int(time.time() * 1000))
            sign = base64.b64encode(hmac.new(self.secret.encode(), (ts + "GET" + req_path).encode(), hashlib.sha256).digest()).decode()
            h = {"ACCESS-KEY": self.key, "ACCESS-SIGN": sign, "ACCESS-TIMESTAMP": ts, "ACCESS-PASSPHRASE": self.pp,
                 "Content-Type": "application/json", "locale": "en-US"}
            try:
                status, body = http_get_json(self.BASE + req_path, h)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                raise ApiError(f"Bitget 연결 실패: {e}")
            code = str(body.get("code", ""))
            if code == "00000":
                return body.get("data")
            if status == 429 or code == "429":
                time.sleep(1.0 * (attempt + 1))
                continue
            if "_raw" in body:
                raise ApiError(f"Bitget HTTP {status}{blocked_hint(status)}")
            raise ApiError(f"Bitget 오류 code={code} {body.get('msg', '')}")
        raise ApiError("Bitget 요청 한도 초과")

    def leverage(self, symbol, product, coin, mode, side):
        """히스토리에 배율이 없어서 현재 설정 배율로 추정 (대부분 고정 배율로 쓰면 일치)."""
        k = (symbol, product)
        if k not in self._lev_cache:
            try:
                d = self.get("/api/v2/mix/account/account", {"symbol": symbol, "productType": product, "marginCoin": coin}) or {}
                self._lev_cache[k] = d
                time.sleep(0.12)
            except ApiError:
                self._lev_cache[k] = {}
        d = self._lev_cache[k]
        if mode == "isolated":
            return num(d.get("isolatedLongLever" if side == "long" else "isolatedShortLever"))
        return num(d.get("crossedMarginLeverage"))

    def fetch(self, stop_ms, known_ids):
        out = []
        for product in self.products:
            id_lt, pages = None, 0
            while pages < 200:
                d = self.get("/api/v2/mix/position/history-position",
                             {"productType": product, "limit": "100", "idLessThan": id_lt}) or {}
                pages += 1
                rows = d.get("list") or []
                if not rows:
                    break
                for r in rows:
                    t = self.normalize(r, product, known_ids)
                    out.append(t)
                oldest = min(int(r["utime"]) for r in rows)
                if (stop_ms and oldest < stop_ms) or len(rows) < 100 or not d.get("endId") or d.get("endId") == id_lt:
                    break
                id_lt = d.get("endId")
                time.sleep(0.15)
        return out

    def normalize(self, r, product, known_ids):
        sym, coin = r.get("symbol", ""), r.get("marginCoin", "")
        base_sym = sym[: -len(coin)] if coin and sym.upper().endswith(coin.upper()) else sym
        mode = "cross" if r.get("marginMode") == "crossed" else (r.get("marginMode") or "")
        side = r.get("holdSide", "")
        open_pos, close_pos = num(r.get("openTotalPos")), num(r.get("closeTotalPos"))
        open_px = num(r.get("openAvgPrice"))
        fee = (num(r.get("openFee")) or 0) + (num(r.get("closeFee")) or 0)
        closed = bool(open_pos and close_pos is not None and close_pos >= open_pos * 0.9999)
        t = {
            "id": f"bg_{r['positionId']}",
            "exchange": "bitget",
            "posId": r["positionId"],
            "instId": sym,
            "base": base_sym,
            "mgnMode": mode,
            "direction": side,
            "ccy": coin,
            "type": "2" if closed else "1",
            "status": "closed" if closed else "partial",
            "cTime": int(r["ctime"]),
            "uTime": int(r["utime"]),
            "openAvgPx": open_px,
            "closeAvgPx": num(r.get("closeAvgPrice")),
            "openMaxPos": open_pos,
            "closeTotalPos": close_pos,
            "posUnit": "coin",
            "pnl": num(r.get("pnl")),
            "fee": round(fee, 8),
            "fundingFee": num(r.get("totalFunding")),
            "liqPenalty": 0.0,
            "realizedPnl": num(r.get("netProfit")),
        }
        if open_pos and open_px:
            t["notional"] = round(open_pos * open_px, 4)
        # 배율은 처음 볼 때만 조회 (기존 기록은 main()에서 이전 값 유지)
        if t["id"] not in known_ids:
            lev = self.leverage(sym, product, coin, mode, side)
            if lev:
                t["lever"], t["leverEst"] = lev, True
        return t

    def open_positions(self):
        """보유 중 포지션. totalFee = 포지션 보유 중 누적 펀딩비."""
        out = []
        for product in self.products:
            for r in self.get("/api/v2/mix/position/all-position", {"productType": product}) or []:
                if not num(r.get("total")):
                    continue
                sym = r.get("symbol", "")
                qty, avg, mark, margin = num(r.get("total")), num(r.get("openPriceAvg")), num(r.get("markPrice")), num(r.get("marginSize"))
                upl = num(r.get("unrealizedPL")) or 0.0
                out.append({"id": f"bg_{sym}_{r.get('holdSide')}_{r.get('cTime')}", "instId": sym, "base": sym.replace("USDT", "").replace("USDC", "").replace("USD", ""),
                            "direction": r.get("holdSide", ""), "lever": num(r.get("leverage")), "mgnMode": r.get("marginMode"), "cTime": int(r.get("cTime") or 0),
                            "qty": qty, "contracts": None, "avgPx": avg, "markPx": mark, "liqPx": num(r.get("liquidationPrice")),
                            "upl": round(upl, 4), "uplRatio": (upl / margin) if margin else None, "margin": margin,
                            "notional": round(qty * mark, 2) if qty and mark else None, "ccy": r.get("marginCoin", "USDT"),
                            "fundingFee": round(num(r.get("totalFee")) or 0.0, 6), "fee": None})
        return out

    def balance(self):
        eq = avail = margin = upl = 0.0
        for product in self.products:
            for a in self.get("/api/v2/mix/account/accounts", {"productType": product}) or []:
                eq += num(a.get("usdtEquity")) or num(a.get("accountEquity")) or 0.0
                avail += num(a.get("crossedMaxAvailable")) or num(a.get("available")) or 0.0
                margin += num(a.get("locked")) or 0.0
                upl += num(a.get("unrealizedPL")) or 0.0
        return {"equity": round(eq, 2), "avail": round(avail, 2), "margin": round(margin, 2), "upl": round(upl, 2)}

    def equity(self):
        total = 0.0
        for product in self.products:
            for a in self.get("/api/v2/mix/account/accounts", {"productType": product}) or []:
                total += num(a.get("usdtEquity")) or num(a.get("accountEquity")) or 0.0
        return total


# ═════════════════════════════ 공통 ═════════════════════════════
def load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def dump_json(p: Path, obj) -> bool:
    """내용이 바뀐 경우에만 기록 → 불필요한 커밋 방지"""
    new = json.dumps(obj, ensure_ascii=False, indent=1) + "\n"
    if p.exists() and p.read_text(encoding="utf-8") == new:
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(new, encoding="utf-8")
    return True


def fill_derived(t, prev):
    """이전 기록의 추정값 유지 + 증거금/수익률 보완"""
    if prev:
        for k in ("notional", "lever", "leverEst"):
            if t.get(k) is None and prev.get(k) is not None:
                t[k] = prev[k]
    if t.get("notional") and t.get("lever") and not t.get("margin"):
        t["margin"] = round(t["notional"] / t["lever"], 4)
    if t.get("pnlRatio") is None and t.get("margin") and t.get("realizedPnl") is not None:
        t["pnlRatio"] = round(t["realizedPnl"] / t["margin"], 6)
    return t


def fetch_fx():
    """원화 환산용 USDT 시세. 업비트 KRW-USDT 우선, 안 되면 달러 환율로 대신."""
    srcs = [
        ("업비트 USDT", "https://api.upbit.com/v1/ticker?markets=KRW-USDT", lambda j: j[0]["trade_price"]),
        ("달러 환율", "https://open.er-api.com/v6/latest/USD", lambda j: j["rates"]["KRW"]),
    ]
    for name, url, pick in srcs:
        try:
            status, body = http_get_json(url, {"Accept": "application/json"}, timeout=10)
            if status == 200:
                v = float(pick(body))
                if 500 < v < 5000:
                    return {"usdt_krw": round(v, 1), "src": name, "ts": int(time.time() * 1000)}
        except Exception as e:  # 환율은 부가 정보라 실패해도 수집은 계속
            print(f"[환율] {name} 실패: {e}")
    return None


def main():
    adapters = [cls() for cls in (OKX, Bitget) if cls.enabled()]
    if not adapters:
        dump_json(STATUS_FILE, {})
        print("[안내] 연결된 거래소가 없어요. 사이트 설정 > 거래소 연결에서 API 키를 등록하세요.")
        return

    store = load_json(TRADES_FILE, {"version": 2, "trades": []})
    by_id = {}
    for t in store.get("trades", []):
        t.setdefault("exchange", "okx")
        t.setdefault("base", t.get("instId", "").split("-")[0])
        by_id[t["id"]] = t

    equity = load_json(EQUITY_FILE, {"days": {}})
    today = dt.datetime.now(KST).strftime("%Y-%m-%d")
    day = equity.setdefault("days", {}).setdefault(today, {})
    if "open" in day and "by" not in day:  # v1 형식(OKX 단일) 호환
        day["by"] = {"okx": day["open"]}
    day.setdefault("by", {})

    status, ok_count = {}, 0
    for ad in adapters:
        mine = [t for t in by_id.values() if t.get("exchange") == ad.name]
        max_u = max((t["uTime"] for t in mine), default=None)
        stop = (max_u - OVERLAP_MS) if max_u else None
        print(f"[{ad.name}] 기존 {len(mine)}건, {'증분' if stop else '전체 백필(최근 3개월)'}")
        try:
            rows = ad.fetch(stop) if ad.name == "okx" else ad.fetch(stop, set(by_id))
            for t in rows:
                by_id[t["id"]] = fill_derived(t, by_id.get(t["id"]))
            if ad.name not in day["by"]:
                eq = ad.equity()
                if eq is not None:
                    day["by"][ad.name] = round(eq, 2)
            status[ad.name] = {"ok": True}
            ok_count += 1
            print(f"[{ad.name}] 조회 {len(rows)}건")
        except ApiError as e:
            status[ad.name] = {"ok": False, "error": str(e)}
            print(f"[{ad.name}] 실패: {e}")

    if day["by"]:
        day["open"] = round(sum(day["by"].values()), 2)
    else:
        equity["days"].pop(today, None)

    # 수수료 페이백: 거래소별 입금 내역을 id로 중복 없이 누적
    reb = load_json(REBATES_FILE, {})
    for ad in adapters:
        if not hasattr(ad, "rebates") or not status.get(ad.name, {}).get("ok"):
            continue
        have = {x["id"]: x for x in reb.get(ad.name, [])}
        last = max((x["ts"] for x in have.values()), default=None)
        try:
            for x in ad.rebates((last - OVERLAP_MS) if last else None):
                have[x["id"]] = x
            reb[ad.name] = sorted(have.values(), key=lambda x: x["ts"])
            print(f"[{ad.name}] 페이백 {len(reb[ad.name])}건 보관")
        except ApiError as e:
            print(f"[{ad.name}] 페이백 조회 실패: {e}")
    changed_reb = dump_json(REBATES_FILE, reb) if reb else False

    # 지금 계좌 잔고 (실패해도 수집은 계속)
    bal = {}
    for ad in adapters:
        if not status.get(ad.name, {}).get("ok"):
            continue
        try:
            bal[ad.name] = ad.balance()
            print(f"[{ad.name}] 잔고 {bal[ad.name]}")
        except ApiError as e:
            print(f"[{ad.name}] 잔고 조회 실패: {e}")
    if bal:
        prev_bal = load_json(BALANCE_FILE, {})
        same = bal == {k: v for k, v in prev_bal.items() if k != "ts"}
        bal["ts"] = prev_bal.get("ts") if same and prev_bal.get("ts") else int(time.time() * 1000)
        changed_reb |= dump_json(BALANCE_FILE, bal)

    # 보유 중 포지션의 누적 펀딩비 (실패해도 수집은 계속)
    prev_open = load_json(OPEN_FILE, {})
    opened = {k: v for k, v in prev_open.items() if k != "ts"}
    for ad in adapters:
        if not status.get(ad.name, {}).get("ok"):
            continue
        try:
            opened[ad.name] = ad.open_positions()
            print(f"[{ad.name}] 보유 중 {len(opened[ad.name])}개, 누적 펀딩비 {sum(x['fundingFee'] for x in opened[ad.name]):+.4f}")
        except ApiError as e:
            print(f"[{ad.name}] 보유 포지션 조회 실패: {e}")
    for k in [k for k in opened if k != "ts" and k not in {a.name for a in adapters}]:
        opened.pop(k)
    # 내용이 그대로면 기준 시각도 그대로 (빈 커밋 방지)
    same = opened == {k: v for k, v in prev_open.items() if k != "ts"}
    opened["ts"] = prev_open.get("ts") if same and prev_open.get("ts") else int(time.time() * 1000)
    changed_reb |= dump_json(OPEN_FILE, opened)

    trades = sorted(by_id.values(), key=lambda x: x["uTime"], reverse=True)
    changed = dump_json(TRADES_FILE, {"version": 2, "trades": trades})
    changed |= dump_json(EQUITY_FILE, equity)
    changed |= dump_json(STATUS_FILE, status)
    changed |= changed_reb
    fx = fetch_fx()
    if fx:
        old = load_json(FX_FILE, {})
        # 1원 미만 흔들림이나 10분 안 재조회로는 커밋하지 않음
        if abs((old.get("usdt_krw") or 0) - fx["usdt_krw"]) >= 1 or fx["ts"] - (old.get("ts") or 0) > 6 * 3600 * 1000:
            changed |= dump_json(FX_FILE, fx)
        print(f"[환율] 1 USDT = {fx['usdt_krw']}원 ({fx['src']})")
    if not NOTES_FILE.exists():
        changed |= dump_json(NOTES_FILE, {})
    print(f"[완료] v{VERSION} 총 {len(trades)}건 / 변경 {'있음' if changed else '없음'}")
    if ok_count == 0:
        sys.exit("[오류] 모든 거래소 수집 실패")


if __name__ == "__main__":
    main()
