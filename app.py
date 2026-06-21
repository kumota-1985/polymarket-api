#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PolyFeed API  --  flip_polymarketapi/app.py   (build-to-flip product #2: 予測市場データ)
================================================================================
Polymarket(分散型予測市場)の "集合知オッズ" を、クリーンな1コールで返すAPI。
データは Polymarket の公開API(認証不要)+ Polygon上の公開データ由来 → 再配布ToS問題なし。

  - /v1/markets : アクティブな予測市場一覧(現在オッズ・出来高・決着日)        ← Gamma
  - /v1/price   : 指定トークン(YES/NO)の最新価格(=確率)                    ← CLOB
  - /v1/history : オッズの時系列 + ?as_of=YYYY-MM-DD で Point-in-Time         ← CLOB
差別化 = クリーンなスキーマ・JSON/CSV・point-in-time・1コール・freemium。

  pip install -r requirements.txt
  uvicorn app:app --reload --port 8002   → http://127.0.0.1:8002/docs  (X-API-Key: DEMO)
"""
import os
import io
import csv
import json
import time
import base64
import hashlib
import hmac
import threading
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from typing import Optional, List

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse, JSONResponse, HTMLResponse

GAMMA = "https://gamma-api.polymarket.com/markets"
CLOB_HIST = "https://clob.polymarket.com/prices-history"
BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://polymarket-api.onrender.com")

DEMO_MARKETS = 10        # DEMO: 返す市場数の上限
DEMO_POINTS = 100        # DEMO: 履歴点数の上限
PRO_KEYS = set(k.strip() for k in os.environ.get("POLYMARKET_KEYS", "").split(",") if k.strip())
RAPIDAPI_SECRET = os.environ.get("RAPIDAPI_PROXY_SECRET")
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
KEY_SIGNING_SECRET = os.environ.get("KEY_SIGNING_SECRET", "")
DIRECT_AUTO = bool(STRIPE_SECRET_KEY and KEY_SIGNING_SECRET)

_cache = {}        # url -> (data, expiry)
_sub_cache = {}    # sub_id -> (active, expiry)
_CACHE_MAX = int(os.environ.get("POLYFEED_CACHE_MAX", "512"))   # url キャッシュの最大件数(メモリ肥大化防止)
_cache_lock = threading.Lock()   # FastAPIは同期defをスレッドプールで並列実行 → 退避処理を直列化


def _cache_put(url, data, expiry):
    """url->(data,expiry) を保存。上限到達時はまず期限切れを掃除し、それでも超過なら最古から削除。"""
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            now = time.time()
            for k in [k for k, v in _cache.items() if v[1] <= now]:
                _cache.pop(k, None)
            while len(_cache) >= _CACHE_MAX:
                _cache.pop(next(iter(_cache)), None)   # dict は挿入順 → 最古を退避
        _cache[url] = (data, expiry)


# --------------------------------------------------------------------------- #
#  直販APIキー(HMAC署名・DB不要・解約で自動失効)— CleanQuant と同方式         #
# --------------------------------------------------------------------------- #
def _b64(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def mint_key(sub_id: str) -> str:
    payload = _b64(sub_id.encode())
    sig = hmac.new(KEY_SIGNING_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"pf_{payload}.{sig}"


def _key_sub_id(key: str):
    if not key or not key.startswith("pf_") or "." not in key:
        return None
    payload, _, sig = key[3:].partition(".")
    expect = hmac.new(KEY_SIGNING_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expect):
        return None
    try:
        return _unb64(payload).decode()
    except Exception:
        return None


def _sub_active(sub_id: str) -> bool:
    now = time.time()
    c = _sub_cache.get(sub_id)
    if c and c[1] > now:
        return c[0]
    active = False
    try:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY
        active = stripe.Subscription.retrieve(sub_id).get("status") in ("active", "trialing")
    except Exception:
        active = False
    _sub_cache[sub_id] = (active, now + 600)
    return active


def verify_direct_key(key: str) -> bool:
    if not DIRECT_AUTO:
        return False
    sub_id = _key_sub_id(key)
    return bool(sub_id and _sub_active(sub_id))


def auth(x_api_key: Optional[str], rapid_secret: Optional[str] = None) -> bool:
    """True=pro(full) / False=demo(capped)。RapidAPIプロキシ / 直販鍵(手動・Stripe自動) / DEMO。"""
    if RAPIDAPI_SECRET and rapid_secret == RAPIDAPI_SECRET:
        return True
    if x_api_key and x_api_key in PRO_KEYS:
        return True
    if isinstance(x_api_key, str) and x_api_key.startswith("pf_") and verify_direct_key(x_api_key):
        return True
    if x_api_key == "DEMO":
        return False
    raise HTTPException(status_code=401,
                        detail="Missing/invalid API key. Use 'DEMO' to try, or subscribe for a key.")


# --------------------------------------------------------------------------- #
#  Polymarket 公開APIの取得(リトライ + キャッシュ)                            #
# --------------------------------------------------------------------------- #
def _get_json(url: str, ttl: int = 60):
    now = time.time()
    c = _cache.get(url)
    if c and c[1] > now:
        return c[0]
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    last = None
    for a in range(4):
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                data = json.loads(r.read().decode())
            _cache_put(url, data, now + ttl)
            return data
        except Exception as e:
            last = e
            time.sleep(1.0 * (a + 1))
    raise HTTPException(status_code=502, detail=f"upstream fetch failed: {repr(last)[:120]}")


def _jarr(v):
    """Gamma の outcomes 等は JSON文字列 or 配列の両方がありうる。配列に正規化。"""
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return []
    return []


def _check_format(fmt: str):
    if fmt not in ("json", "csv"):
        raise HTTPException(status_code=422, detail="format must be 'json' or 'csv'")


def _respond(rows: List[dict], fmt: str, pro: bool, cap: int, extra: dict = None):
    if not pro and cap and len(rows) > cap:
        rows = rows[:cap]
    if fmt == "csv":
        if not rows:
            return PlainTextResponse("", media_type="text/csv")
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
        return PlainTextResponse(buf.getvalue(), media_type="text/csv")
    body = {"count": len(rows), "tier": "pro" if pro else "demo", "data": rows}
    if extra:
        body.update(extra)
    return JSONResponse(body)


app = FastAPI(
    title="PolyFeed API",
    version="1.0.0",
    description="Prediction-market data from Polymarket: live odds, current prices, and "
                "point-in-time historical odds (?as_of=). Clean schema, JSON or CSV. "
                "Built on Polymarket's public APIs.",
)


# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def root():
    return ('<h2>PolyFeed API</h2><p>Prediction-market odds from Polymarket — live, historical, '
            'and point-in-time.</p><p>See <a href="/docs">/docs</a>. Try header '
            '<code>X-API-Key: DEMO</code>. Datasets: <a href="/v1/catalog">/v1/catalog</a></p>')


@app.get("/llms.txt", response_class=PlainTextResponse, include_in_schema=False)
def llms_txt():
    """AIエージェント/検索AI 向けの自己紹介(自動発見・推薦の導線)。"""
    return f"""# PolyFeed API
> Polymarket prediction-market odds: live, historical, point-in-time (?as_of=).

Base URL: {BASE_URL}
Docs: {BASE_URL}/docs
OpenAPI: {BASE_URL}/openapi.json

## Endpoints
- GET /v1/markets - active prediction markets with live odds & volume
- GET /v1/price?token_id=... - latest price (implied probability) for a YES/NO token
- GET /v1/history?token_id=...&as_of=YYYY-MM-DD - odds time series, point-in-time
- Auth: header X-API-Key (use DEMO to try)
"""


@app.get("/v1/activate", response_class=HTMLResponse, include_in_schema=False)
def activate(session_id: str = ""):
    if not DIRECT_AUTO:
        return HTMLResponse("<p>Key auto-issue is not configured on this server.</p>", status_code=503)
    if not session_id:
        return HTMLResponse("<p>Missing session_id.</p>", status_code=400)
    try:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY
        sess = stripe.checkout.Session.retrieve(session_id)
    except Exception:
        return HTMLResponse("<p>Could not verify your checkout session.</p>", status_code=400)
    paid = sess.get("payment_status") == "paid" or sess.get("status") == "complete"
    sub_id = sess.get("subscription")
    if not (paid and sub_id):
        return HTMLResponse("<p>Payment not completed yet. If you just paid, refresh in a moment.</p>",
                            status_code=402)
    key = mint_key(sub_id)
    return HTMLResponse(
        "<html><body style='font-family:sans-serif;max-width:680px;margin:48px auto;color:#1a1f2b'>"
        "<h2>&#9989; Subscription active &mdash; your API key</h2>"
        "<p>Send it in the <code>X-API-Key</code> header. Keep it safe.</p>"
        f"<pre style='background:#f1f3f7;padding:14px;border-radius:8px;user-select:all'>{key}</pre>"
        f"<pre style='background:#f1f3f7;padding:14px;border-radius:8px'>curl -H \"X-API-Key: {key}\" \\\n"
        f"  \"{BASE_URL}/v1/markets\"</pre>"
        "<p style='color:#5b6472'>Access stays active while your subscription is active.</p>"
        "</body></html>")


@app.get("/v1/catalog")
def catalog():
    return {"datasets": {
        "markets": {"desc": "Active prediction markets (live odds, volume, end date)",
                    "endpoint": "/v1/markets", "source": "Polymarket Gamma API"},
        "price": {"desc": "Latest price (= implied probability) for a YES/NO token",
                  "endpoint": "/v1/price", "source": "Polymarket CLOB"},
        "history": {"desc": "Odds time series, with ?as_of= point-in-time",
                    "endpoint": "/v1/history", "source": "Polymarket CLOB"}},
        "auth": {"demo_key": "DEMO", "demo_markets_cap": DEMO_MARKETS, "demo_points_cap": DEMO_POINTS}}


@app.get("/v1/markets")
def markets(limit: int = Query(50, ge=1, le=100), closed: bool = False,
            min_volume: float = 0.0, format: str = "json",
            x_api_key: Optional[str] = Header(None),
            x_rapidapi_proxy_secret: Optional[str] = Header(None)):
    """アクティブ(または closed=true で解決済み)な予測市場一覧。現在オッズ・出来高つき。"""
    _check_format(format)
    pro = auth(x_api_key, x_rapidapi_proxy_secret)
    url = (f"{GAMMA}?closed={'true' if closed else 'false'}&active={'false' if closed else 'true'}"
           f"&order=volumeNum&ascending=false&limit={min(limit, 100)}")
    raw = _get_json(url, ttl=60)
    rows = []
    for m in raw if isinstance(raw, list) else []:
        outs, prices, toks = _jarr(m.get("outcomes")), _jarr(m.get("outcomePrices")), _jarr(m.get("clobTokenIds"))
        vol = float(m.get("volumeNum") or 0)
        if vol < min_volume:
            continue
        rows.append({
            "id": m.get("conditionId"), "question": m.get("question"),
            "outcomes": outs, "prices": [float(p) for p in prices if p not in (None, "")],
            "token_ids": toks, "volume": vol, "end_date": m.get("endDate"),
            "active": m.get("active"), "closed": m.get("closed")})
    return _respond(rows, format, pro, DEMO_MARKETS)


@app.get("/v1/price")
def price(token_id: str = Query(...), format: str = "json",
          x_api_key: Optional[str] = Header(None),
          x_rapidapi_proxy_secret: Optional[str] = Header(None)):
    """指定トークン(YES/NO の clobTokenId)の最新価格(=市場が織り込む確率)。"""
    _check_format(format)
    pro = auth(x_api_key, x_rapidapi_proxy_secret)
    h = _get_json(f"{CLOB_HIST}?market={urllib.parse.quote(token_id)}&interval=1d&fidelity=10",
                  ttl=30).get("history", [])
    if not h:
        h = _get_json(f"{CLOB_HIST}?market={urllib.parse.quote(token_id)}&interval=max&fidelity=720",
                      ttl=60).get("history", [])
    if not h:
        raise HTTPException(status_code=404, detail="no price data for token_id")
    last = h[-1]
    row = [{"token_id": token_id, "price": float(last["p"]),
            "time": datetime.fromtimestamp(int(last["t"]), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}]
    return _respond(row, format, pro, 1)


@app.get("/v1/history")
def history(token_id: str = Query(...), interval: str = "max", fidelity: int = 720,
            as_of: Optional[str] = None, format: str = "json",
            x_api_key: Optional[str] = Header(None),
            x_rapidapi_proxy_secret: Optional[str] = Header(None)):
    """オッズの時系列。as_of=YYYY-MM-DD でその時点までの観測のみ(Point-in-Time)。"""
    _check_format(format)
    pro = auth(x_api_key, x_rapidapi_proxy_secret)
    if interval not in ("1d", "1w", "1m", "max"):
        raise HTTPException(status_code=422, detail="interval must be 1d, 1w, 1m, or max")
    h = _get_json(f"{CLOB_HIST}?market={urllib.parse.quote(token_id)}"
                  f"&interval={interval}&fidelity={max(1, min(fidelity, 720))}", ttl=120).get("history", [])
    cutoff = None
    if as_of:
        try:
            c = datetime.fromisoformat(as_of.replace("Z", "+00:00")) if "T" in as_of else \
                datetime.strptime(as_of, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            raise HTTPException(status_code=422, detail="as_of must be a date (YYYY-MM-DD)")
        cutoff = c.timestamp()
    rows = []
    for pt in h:
        t = int(pt["t"])
        if cutoff is not None and t > cutoff:
            continue
        rows.append({"time": datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     "price": float(pt["p"])})
    return _respond(rows, format, pro, DEMO_POINTS, extra={"token_id": token_id})
