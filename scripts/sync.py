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

KST = dt.timezone(dt.timedelta(hours=9))
UA = "trade-journal-sync/2.0"
# 증분 동기화 시 마지막 업데이트보다 이만큼 과거까지 다시 훑음 (부분청산 → 전체청산 갱신 대비)
OVERLAP_MS = 3 * 24 * 60 * 60 * 1000


class ApiError(RuntimeError):
    pass


VERSION = "3"
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


def okx_bill_survey(ad):
    """수수료 페이백이 계좌에 어떤 형태로 들어오는지 확인용 (한 번만 실행, 종류·건수·합계만 기록)"""
    out = {"account_types": {}, "account": {}, "funding": {}}
    try:
        for t in ad.get("/api/v5/account/subtypes") or []:
            out["account_types"][t["type"]] = {"desc": t.get("typeDesc", ""),
                                               "sub": {x["subType"]: x.get("subTypeDesc", "") for x in t.get("subTypeDetails", [])}}
    except ApiError as e:
        out["account_types_error"] = str(e)
    after, pages = None, 0
    while pages < 80:
        try:
            rows = ad.get("/api/v5/account/bills-archive", {"limit": "100", "after": after})
        except ApiError as e:
            out["account_error"] = str(e); break
        pages += 1
        if not rows: break
        for r in rows:
            k = f"{r.get('type')}/{r.get('subType')}/{r.get('ccy')}"
            g = out["account"].setdefault(k, {"n": 0, "balChg": 0.0, "fee": 0.0, "first": None, "last": None})
            g["n"] += 1; g["balChg"] += num(r.get("balChg")) or 0; g["fee"] += num(r.get("fee")) or 0
            ts = int(r.get("ts") or 0); g["first"] = min(g["first"] or ts, ts); g["last"] = max(g["last"] or ts, ts)
        if len(rows) < 100: break
        after = rows[-1]["billId"]; time.sleep(0.45)
    out["account_pages"] = pages
    after, pages = None, 0
    while pages < 30:
        try:
            rows = ad.get("/api/v5/asset/bills", {"limit": "100", "after": after})
        except ApiError as e:
            out["funding_error"] = str(e); break
        pages += 1
        if not rows: break
        for r in rows:
            k = f"{r.get('type')}/{r.get('ccy')}"
            g = out["funding"].setdefault(k, {"n": 0, "balChg": 0.0, "first": None, "last": None, "notes": set()})
            g["n"] += 1; g["balChg"] += num(r.get("balChg")) or 0
            ts = int(r.get("ts") or 0); g["first"] = min(g["first"] or ts, ts); g["last"] = max(g["last"] or ts, ts)
            if r.get("notes"): g["notes"].add(r["notes"][:40])
        if len(rows) < 100: break
        after = rows[-1]["ts"]; time.sleep(0.6)
    for g in out["funding"].values(): g["notes"] = sorted(g["notes"])[:5]
    for g in list(out["account"].values()) + list(out["funding"].values()):
        g["balChg"] = round(g["balChg"], 6); g.get("fee") is not None and g.__setitem__("fee", round(g["fee"], 6))
    dump_json(DATA / "bill_survey.json", out)
    print("[조사] 계좌 내역 종류", len(out["account"]), "/ 자금 계좌 내역 종류", len(out["funding"]))


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

    for ad in adapters:
        if ad.name == "okx" and not (DATA / "bill_survey.json").exists():
            okx_bill_survey(ad)

    trades = sorted(by_id.values(), key=lambda x: x["uTime"], reverse=True)
    changed = dump_json(TRADES_FILE, {"version": 2, "trades": trades})
    changed |= dump_json(EQUITY_FILE, equity)
    changed |= dump_json(STATUS_FILE, status)
    if not NOTES_FILE.exists():
        changed |= dump_json(NOTES_FILE, {})
    print(f"[완료] v{VERSION} 총 {len(trades)}건 / 변경 {'있음' if changed else '없음'}")
    if ok_count == 0:
        sys.exit("[오류] 모든 거래소 수집 실패")


if __name__ == "__main__":
    main()
