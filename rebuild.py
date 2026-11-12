#!/usr/bin/env python3
"""rebuild.py - Global Liquidity Lead Indicator Model.

Regenerates index.html and signal.csv from an empty directory:
    pip install -r requirements.txt
    python rebuild.py            # live fetch; caches raw downloads in ./raw/
    python rebuild.py --offline  # rebuild from ./raw/ without network

Optional: set FRED_API_KEY (free key from fred.stlouisfed.org) to use FRED's official API, which is
more reliable from cloud runners. Without it the public CSV endpoint is used.

If a live download fails, the last good copy in ./raw/ is used and the page says so. The script exits
non-zero WITHOUT writing any output when a series has no data at all, the signal cannot be computed,
or the latest indicator month is more than three months old, so a scheduled job never publishes a
broken page.

Sources: FRED, ECB Data Portal, Eurostat, BoJ, e-Stat, BoE, ONS, BIS, MSCI, East Money, Yahoo Finance.
"""
import os, io, re, json, time, csv
import threading
import pandas as pd, numpy as np, requests
_FRED_LOCK = threading.Lock()

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "raw")
os.makedirs(RAW, exist_ok=True)
H = {"User-Agent": "Mozilla/5.0 (research dashboard rebuild)"}
OFFLINE = False
STALE = set()       # cache keys served from the last good download on this build
FALLBACKS = {}      # series that used a secondary source on this build


def _redact(msg):
    """Strip the FRED API key from anything that may reach logs or the published page."""
    msg = str(msg)
    k = os.environ.get("FRED_API_KEY", "").strip()
    return msg.replace(k, "***") if k else msg


def _path(key):
    return os.path.join(RAW, re.sub(r"[^A-Za-z0-9_.-]", "_", key))


def _stale(key, binary=False, why=""):
    path = _path(key)
    if os.path.exists(path) and open(path, "rb").read(1) != b"<":
        print(f"  ! {key}: live fetch failed ({_redact(why)[:70]}), using last good download")
        STALE.add(key)
        return open(path, "rb").read() if binary else open(path, encoding="utf-8").read()
    raise RuntimeError(f"{key}: {_redact(why)[:120]} (no cached copy)")


def _get(key, url, timeout=60, tries=3, binary=False, headers=None, allow_stale=True):
    path = _path(key)
    if OFFLINE and os.path.exists(path):
        return open(path, "rb").read() if binary else open(path, encoding="utf-8").read()
    err = None
    for i in range(tries):
        try:
            r = requests.get(url, timeout=timeout, headers=H if headers is None else headers)
            r.raise_for_status()
            if not binary and key.endswith((".csv", ".json")) and r.text.lstrip()[:1] == "<":
                raise RuntimeError("got an HTML error page instead of data")
            if binary:
                open(path, "wb").write(r.content); return r.content
            open(path, "w", encoding="utf-8").write(r.text); return r.text
        except Exception as e:
            err = e; time.sleep(2 + 3 * i)
    if allow_stale:
        return _stale(key, binary, str(err))
    raise RuntimeError(f"{key}: {_redact(err)[:120]}")


def fred(sid):
    """FRED series. Uses the official API when FRED_API_KEY is set (more reliable from cloud runners),
    otherwise the public CSV endpoint. Either falls back to the last good download."""
    key = os.environ.get("FRED_API_KEY", "").strip()
    with _FRED_LOCK:  # FRED throttles parallel requests; serialise and pace
        if key and not OFFLINE:
            try:
                t = _get(f"fredapi_{sid}.json", f"https://api.stlouisfed.org/fred/series/observations?series_id={sid}&api_key={key}&file_type=json",
                         30, tries=3, headers={}, allow_stale=False)
                time.sleep(0.6)
                obs = json.loads(t)["observations"]
                s = pd.Series(pd.to_numeric([o["value"] for o in obs], errors="coerce"), index=pd.to_datetime([o["date"] for o in obs]))
                return s.dropna()
            except Exception as e:
                print(f"  ! FRED API failed for {sid} ({_redact(e)[:60]}), trying the CSV endpoint")
        # FRED's edge tarpits browser-style user agents from scripts; send the plain client header
        t = _get(f"fred_{sid}.csv", f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}", 30, tries=4, headers={})
        if not OFFLINE: time.sleep(0.3)
    d = pd.read_csv(io.StringIO(t)); d.columns = ["date", "v"]
    s = pd.to_numeric(d["v"], errors="coerce"); s.index = pd.to_datetime(d["date"])
    return s.dropna()


def ecb(flow, key):
    t = _get(f"ecb_{flow}_{key}.csv", f"https://data-api.ecb.europa.eu/service/data/{flow}/{key}?format=csvdata", 150)
    d = pd.read_csv(io.StringIO(t), usecols=["TIME_PERIOD", "OBS_VALUE"])
    tp = d["TIME_PERIOD"].astype(str)
    if tp.str.contains("Q").any():
        idx = pd.PeriodIndex(tp.str.replace("-", ""), freq="Q").to_timestamp()
    else:
        idx = pd.to_datetime(tp)
    return pd.Series(pd.to_numeric(d["OBS_VALUE"], errors="coerce").values, index=idx).dropna()


def boj(db, code, start="200001"):
    t = _get(f"boj_{code}.csv", f"https://www.stat-search.boj.or.jp/api/v1/getDataCode?format=csv&lang=en&db={db}&code={code}&startDate={start}", 90)
    out = {}
    for row in csv.reader(io.StringIO(t)):
        if row and row[0] == code and len(row) >= 3 and re.fullmatch(r"\d{6}", row[-2] or ""):
            try: out[pd.Timestamp(row[-2][:4] + "-" + row[-2][4:] + "-01")] = float(row[-1])
            except ValueError: pass
    s = pd.Series(out).sort_index()
    if s.empty: raise RuntimeError("BoJ parse empty")
    return s


def eastmoney(report, cols):
    t = _get(f"em_{report}.json", f"https://datacenter-web.eastmoney.com/api/data/v1/get?reportName={report}&columns=ALL&pageSize=500&pageNumber=1&sortColumns=REPORT_DATE&sortTypes=1", 60)
    d = pd.DataFrame(json.loads(t)["result"]["data"])
    d.index = pd.to_datetime(d["REPORT_DATE"])
    return d[cols].apply(pd.to_numeric, errors="coerce").sort_index()


def msci(code="892400"):
    end = pd.Timestamp.today().strftime("%Y%m%d")
    t = _get(f"msci_{code}.json", f"https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?currency_symbol=USD&index_variant=STRD&start_date=19970101&end_date={end}&data_frequency=END_OF_MONTH&index_codes={code}", 60)
    lv = json.loads(t)["indexes"]["INDEX_LEVELS"]
    return pd.Series({pd.to_datetime(str(x["calc_date"])): x["level_eod"] for x in lv}).sort_index()


def yahoo_monthly(sym):
    key, t, err = f"yahoo_{sym}.json", None, ""
    if OFFLINE and os.path.exists(_path(key)):
        t = open(_path(key), encoding="utf-8").read()
    for host in ([] if t else ["query2", "query1"]):  # Yahoo rate-limits one host more than the other
        try:
            t = _get(key, f"https://{host}.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(sym)}?range=max&interval=1mo", 60, allow_stale=False)
            break
        except Exception as e:
            err = str(e)
    if t is None:
        t = _stale(key, why=err)
    r = json.loads(t)["chart"]["result"][0]
    s = pd.Series(r["indicators"]["quote"][0]["close"], index=pd.to_datetime(r["timestamp"], unit="s"), dtype=float)
    s.index = s.index.to_period("M").to_timestamp()
    return s.dropna().groupby(level=0).last()


def bis_neer(area="US"):
    t = _get(f"bis_neer_{area}.csv", f"https://stats.bis.org/api/v2/data/dataflow/BIS/WS_EER/1.0/M.N.B.{area}?format=csv", 60)
    d = pd.read_csv(io.StringIO(t), usecols=["TIME_PERIOD", "OBS_VALUE"])
    return pd.Series(pd.to_numeric(d["OBS_VALUE"], errors="coerce").values, index=pd.to_datetime(d["TIME_PERIOD"])).dropna().sort_index()


def ons(uri, key):
    t = _get(f"ons_{key}.csv", f"https://www.ons.gov.uk/generator?format=csv&uri={uri}", 60)
    out = {}
    for row in csv.reader(io.StringIO(t)):
        if len(row) == 2 and re.fullmatch(r"\d{4} [A-Z]{3}", row[0]):
            try: out[pd.to_datetime(row[0], format="%Y %b")] = float(row[1])
            except ValueError: pass
    return pd.Series(out).sort_index()


def jp_core_cpi():
    """e-Stat long-run CPI table (2020 base). Column: All items, less fresh food and energy."""
    b = _get("estat_jp_cpi.csv", "https://www.e-stat.go.jp/stat-search/file-download?statInfId=000032103842&fileKind=1", 90, binary=True)
    out = {}
    for row in csv.reader(io.StringIO(b.decode("shift_jis", errors="replace"))):
        if row and re.fullmatch(r"\d{6}", row[0]) and len(row) > 5 and row[5]:
            out[pd.Timestamp(row[0][:4] + "-" + row[0][4:] + "-01")] = float(row[5])
    return pd.Series(out).sort_index()


def boe(code):
    t = _get(f"boe_{code}.csv", f"https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp?csv.x=yes&Datefrom=01/Jan/2000&Dateto=now&SeriesCodes={code}&CSVF=TN&UsingCodes=Y", 60)
    d = pd.read_csv(io.StringIO(t)); d.columns = ["date", "v"]
    return pd.Series(pd.to_numeric(d["v"], errors="coerce").values, index=pd.to_datetime(d["date"], format="%d %b %Y")).dropna()


def eurostat_rate(dataset, **dims):
    q = "&".join(f"{k}={v}" for k, v in dims.items())
    t = _get(f"estat_{dataset}_{'_'.join(dims.values())}.json", f"https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/{dataset}?{q}&format=JSON", 120)
    j = json.loads(t); inv = {i: k for k, i in j["dimension"]["time"]["category"]["index"].items()}
    return pd.Series({pd.Timestamp(inv[int(k)] + "-01"): float(v) for k, v in j["value"].items()}).sort_index()


def first_of(name, primary, secondary, note):
    """Primary source, else the secondary one (recorded so the page can say so)."""
    try:
        return primary()
    except Exception as e:
        print(f"  ! {name}: primary source failed ({_redact(e)[:60]}), using {note}")
        FALLBACKS[name] = note
        return secondary()


# ======================================================================= model
import concurrent.futures as cf

ME = lambda s: s.groupby(s.index.to_period("M")).last().to_timestamp()      # month-end value, month-start label
MA = lambda s: s.groupby(s.index.to_period("M")).mean().to_timestamp()      # monthly average

# --- fixed weights (approximate; documented on the page) --------------------------------------
W_PPP = {"US": 14.9, "CN": 19.3, "EZ": 11.9, "IN": 8.2, "JP": 3.4, "UK": 2.2, "CA": 1.3, "KR": 1.6}   # IMF WEO PPP shares, %
W_LF = {"US": 170, "EZ": 171, "JP": 69, "UK": 34, "CA": 22}                                          # labour force, mn
W_RATE = {"US": .50, "DE": .30, "JP": .13, "UK": .07}                                                # ~Global Agg currency mix ex-CNY
W_CVIX = {"EURUSD": 35.9, "USDJPY": 21.0, "GBPUSD": 13.1, "AUDUSD": 6.7, "USDCHF": 5.4, "USDCAD": 4.8,
          "EURJPY": 4.3, "EURGBP": 4.1, "EURCHF": 4.7}                                               # turnover-style weights

IND = [  # key, display label, proxy description, transform
    ("stock", "Global stock market cap", "MSCI ACWI price index (USD)", "pct"),
    ("crypto", "Crypto market", "Bitcoin (Coinbase BTC-USD)", "pct"),
    ("usd", "USD nominal effective FX", "BIS broad nominal effective exchange rate, US", "pct"),
    ("cvix", "CVIX", "63-day realised vol of 9 major FX crosses, turnover-weighted", "pct"),
    ("rate", "Global aggregate interest rate", "10y yields US/DE/JP/UK, Global Agg currency weights", "diff"),
    ("move", "MOVE index", "ICE BofA MOVE", "pct"),
    ("gdp", "Global PPP real GDP", "Real GDP YoY, 8 economies, PPP-weighted", "level"),
    ("cpi", "Global core CPI", "Core CPI YoY, US/EZ/JP/UK, PPP-weighted", "level"),
    ("unemp", "Global unemployment rate", "Unemployment rate, US/EZ/JP/UK/CA, labour-force weighted", "diff"),
]
SUM_KEYS = ["usd", "cvix", "rate", "move", "gdp", "cpi", "unemp"]


def fetch_all(log):
    jobs = {
        # FX
        "EURUSD": lambda: fred("DEXUSEU"), "USDJPY": lambda: fred("DEXJPUS"), "USDCNY": lambda: fred("DEXCHUS"),
        "GBPUSD": lambda: fred("DEXUSUK"), "USDCHF": lambda: fred("DEXSZUS"), "USDCAD": lambda: fred("DEXCAUS"),
        "AUDUSD": lambda: fred("DEXUSAL"),
        # central bank balance sheets
        "fed": lambda: fred("WALCL"), "ecb": lambda: fred("ECBASSETSW"), "boj": lambda: fred("JPNASSETS"),
        # broad money
        "m2_us": lambda: fred("M2SL"), "m3_ez": lambda: ecb("BSI", "M.U2.Y.V.M30.X.1.U2.2300.Z01.E"),
        "m2_jp": lambda: boj("MD02", "MAM1NAM2M2MO"),
        "m2_cn": lambda: eastmoney("RPT_ECONOMY_CURRENCY_SUPPLY", ["BASIC_CURRENCY"])["BASIC_CURRENCY"],
        "m2_cn_old": lambda: fred("MYAGM2CNM189N"),
        "m4_uk": lambda: boe("LPMAUYN"),
        # FX reserves ex gold (IMF IFS via FRED)
        **{f"res_{c}": (lambda c=c: fred(f"TRESEG{c}M052N")) for c in ["CN", "JP", "IN", "KR", "SA", "BR"]},
        # indicators
        "acwi": lambda: first_of("acwi", lambda: msci("892400"), lambda: yahoo_monthly("ACWI"), "iShares MSCI ACWI ETF price (Yahoo)"), "btc": lambda: fred("CBBTCUSD"), "neer": lambda: bis_neer("US"),
        "move": lambda: yahoo_monthly("^MOVE"), "evz": lambda: fred("EVZCLS"),
        "y_US": lambda: fred("DGS10"), "y_DE": lambda: fred("IRLTLT01DEM156N"),
        "y_JP": lambda: fred("IRLTLT01JPM156N"), "y_UK": lambda: fred("IRLTLT01GBM156N"),
        "gdp_US": lambda: fred("GDPC1"), "gdp_EZ": lambda: fred("CLVMNACSCAB1GQEA19"),
        "gdp_JP": lambda: fred("JPNRGDPEXP"), "gdp_UK": lambda: fred("NGDPRSAXDCGBQ"),
        "gdp_CA": lambda: fred("NGDPRSAXDCCAQ"), "gdp_KR": lambda: fred("NGDPRSAXDCKRQ"),
        "gdp_IN": lambda: fred("INDGDPRQPSMEI"),
        "gdp_CN": lambda: eastmoney("RPT_ECONOMY_GDP", ["DOMESTICL_PRODUCT_BASE", "SUM_SAME"]),
        "cpi_US": lambda: fred("CPILFESL"),
        "cpi_EZ": lambda: eurostat_rate("prc_hicp_minr", geo="EA", coicop18="TOT_X_NRG_FOOD", unit="RCH_A"),
        "cpi_JP": lambda: jp_core_cpi(),
        "cpi_UK": lambda: ons("/economy/inflationandpriceindices/timeseries/dko8/mm23", "dko8"),
        "u_US": lambda: fred("UNRATE"), "u_EZ": lambda: ecb("LFSI", "M.I9.S.UNEHRT.TOTAL0.15_74.T"),
        "u_JP": lambda: fred("LRUN64TTJPM156S"), "u_CA": lambda: fred("LRUNTTTTCAM156S"),
        "u_UK": lambda: ons("/employmentandlabourmarket/peoplenotinwork/unemployment/timeseries/mgsx/lms", "mgsx"),
    }
    raw, status = {}, {}
    def run(k):
        try: return k, jobs[k](), None
        except Exception as e: return k, None, _redact(e)[:90]
    with cf.ThreadPoolExecutor(10) as ex:
        for k, s, err in ex.map(run, jobs):
            if err: status[k] = "FAILED: " + err; log(f"  x {k:8s} {err}")
            else:
                raw[k] = s; last = s.index[-1]
                status[k] = f"{s.index[0]:%Y-%m} to {last:%Y-%m}"
    return raw, status


def china_quarterly_gdp(d):
    """East Money gives cumulative YTD nominal level and cumulative real YoY. Back out single-quarter real YoY
    using prior-year single-quarter nominal GDP as weights (standard approximation)."""
    d = d.copy(); d["y"] = d.index.year; d["q"] = (d.index.month - 1) // 3 + 1
    lvl = d.set_index(["y", "q"])["DOMESTICL_PRODUCT_BASE"]; cum = d.set_index(["y", "q"])["SUM_SAME"] / 100
    out = {}
    for (y, q), r in cum.items():
        if q == 1: out[pd.Timestamp(y, 1, 1)] = r * 100; continue
        try:
            Nk, Nk1 = lvl[(y - 1, q)], lvl[(y - 1, q - 1)]; Rk1 = cum[(y, q - 1)]
            out[pd.Timestamp(y, 3 * q - 2, 1)] = (((1 + r) * Nk - (1 + Rk1) * Nk1) / (Nk - Nk1) - 1) * 100
        except KeyError: pass
    return pd.Series(out).sort_index()


def quarterly_to_monthly(s, end, limit=5):
    m = s.copy(); m.index = m.index.to_period("Q").to_timestamp()
    idx = pd.date_range(m.index[0], end, freq="MS")
    return m.reindex(idx).ffill(limit=limit + 2)


def wavg(df, w):
    """Weighted mean across available columns, weights renormalised row by row."""
    ww = pd.Series({k: w[k] for k in df.columns})
    mask = df.notna(); num = (df.fillna(0) * ww).sum(axis=1); den = (mask * ww).sum(axis=1)
    out = num / den; out[den == 0] = np.nan
    return out


def build_panel(raw, log):
    fx = {k: ME(raw[k]) for k in ["EURUSD", "USDJPY", "USDCNY", "GBPUSD", "USDCHF", "USDCAD", "AUDUSD"]}
    # ---------- Global Liquidity Proxy (USD bn) ----------
    comp = {}
    comp["Fed assets"] = ME(raw["fed"]) / 1e3
    comp["ECB assets"] = ME(raw["ecb"]) * fx["EURUSD"] / 1e3
    comp["BoJ assets"] = ME(raw["boj"]) * 0.1 / fx["USDJPY"]
    comp["US M2"] = ME(raw["m2_us"])
    comp["Euro area M3"] = ME(raw["m3_ez"]) * fx["EURUSD"] / 1e3
    comp["Japan M2"] = ME(raw["m2_jp"]) * 0.1 / fx["USDJPY"]
    cn = ME(raw["m2_cn"])
    if "m2_cn_old" in raw:  # chain-splice OECD/FRED history (CNY) onto East Money (100mn CNY) at first overlap month
        old = ME(raw["m2_cn_old"]) / 1e8; ov = cn.index.intersection(old.index)[0]
        cn = pd.concat([old[old.index < ov] * cn[ov] / old[ov], cn])
    comp["China M2"] = cn * 0.1 / fx["USDCNY"]
    comp["UK M4"] = ME(raw["m4_uk"]) * fx["GBPUSD"] / 1e3
    for c in ["CN", "JP", "IN", "KR", "SA", "BR"]:
        comp[f"FX reserves {c}"] = ME(raw[f"res_{c}"]) / 1e3
    C = pd.DataFrame(comp)
    C = C[C.index >= "2003-04-01"]
    limits = {k: (4 if k.startswith("FX reserves") else 0) for k in C.columns}
    for k, lim in limits.items():
        if lim: C[k] = C[k].ffill(limit=lim)
    C = C.dropna()
    groups = {"Central bank assets": ["Fed assets", "ECB assets", "BoJ assets"],
              "Broad money": ["US M2", "Euro area M3", "Japan M2", "China M2", "UK M4"],
              "FX reserves ex gold": [c for c in C.columns if c.startswith("FX reserves")]}
    G = pd.DataFrame({g: C[cols].sum(axis=1) for g, cols in groups.items()})
    liq = G.sum(axis=1)
    carried = {k: str(ME(raw[f"res_{k[-2:]}"]).index[-1].strftime("%Y-%m")) for k in C.columns if k.startswith("FX reserves")}

    # ---------- indicators ----------
    X = {}
    X["stock"] = ME(raw["acwi"])
    X["crypto"] = ME(raw["btc"])
    neer = ME(raw["neer"])
    X["usd"] = neer
    # CVIX proxy: realised vol of crosses
    d = pd.DataFrame({k: raw[k] for k in ["EURUSD", "USDJPY", "GBPUSD", "AUDUSD", "USDCHF", "USDCAD"]}).dropna()
    d["EURJPY"] = d.EURUSD * d.USDJPY; d["EURGBP"] = d.EURUSD / d.GBPUSD; d["EURCHF"] = d.EURUSD * d.USDCHF
    vol = np.log(d).diff().rolling(63).std() * np.sqrt(252) * 100
    cv = (vol[list(W_CVIX)] * pd.Series(W_CVIX)).sum(axis=1, min_count=9) / sum(W_CVIX.values())
    X["cvix"] = ME(cv.dropna())
    evz = ME(raw["evz"]) if "evz" in raw else None
    # rates
    yl = pd.DataFrame({"US": MA(raw["y_US"]), "DE": raw["y_DE"], "JP": raw["y_JP"], "UK": raw["y_UK"]})
    yl.index = yl.index.to_period("M").to_timestamp(); yl_raw = yl.groupby(level=0).last(); yl = yl_raw.ffill(limit=2)
    X["rate"] = wavg(yl, W_RATE)
    X["move"] = raw["move"]
    end = max(s.index[-1] for s in [X["stock"], X["usd"], X["cvix"]])
    end = min(end, pd.Timestamp.today().to_period("M").to_timestamp() - pd.offsets.MonthBegin(1))  # last complete month
    # GDP YoY by country (quarterly), then monthly step
    gq = {}
    for c in ["US", "EZ", "JP", "UK", "CA", "KR"]:
        s = raw[f"gdp_{c}"]; s.index = s.index.to_period("Q").to_timestamp(); gq[c] = (s / s.shift(4) - 1) * 100
    gq["IN"] = raw["gdp_IN"]; gq["CN"] = china_quarterly_gdp(raw["gdp_CN"])
    gdpm = pd.DataFrame({c: quarterly_to_monthly(s.dropna(), end) for c, s in gq.items()})
    X["gdp"] = wavg(gdpm, W_PPP)
    # core CPI YoY
    cpi = {}
    for c in ["US", "JP"]:
        s = raw[f"cpi_{c}"]; s.index = s.index.to_period("M").to_timestamp(); cpi[c] = (s / s.shift(12) - 1) * 100
    for c in ["EZ", "UK"]:  # published annual rates
        s = raw[f"cpi_{c}"].copy(); s.index = s.index.to_period("M").to_timestamp(); cpi[c] = s
    cpim = pd.DataFrame(cpi).reindex(pd.date_range("1990-01-01", end, freq="MS")).ffill(limit=2)
    X["cpi"] = wavg(cpim, W_PPP)
    # unemployment
    un = {}
    for c in ["US", "EZ", "JP", "CA", "UK"]:
        s = raw[f"u_{c}"].copy(); s.index = s.index.to_period("M").to_timestamp(); un[c] = s.groupby(level=0).last()
    unm = pd.DataFrame(un).reindex(pd.date_range("1990-01-01", end, freq="MS")).ffill(limit=4)
    X["unemp_lvl"] = wavg(unm, W_LF)

    idx = pd.date_range("1997-01-01", end, freq="MS")
    def al(s): s = s.copy(); s.index = s.index.to_period("M").to_timestamp(); return s.groupby(level=0).last().reindex(idx)
    Xl = pd.DataFrame({k: al(v) for k, v in X.items()})
    # YoY transforms
    Y = pd.DataFrame(index=idx)
    Y["stock"] = (Xl.stock / Xl.stock.shift(12) - 1) * 100
    Y["crypto"] = (Xl.crypto / Xl.crypto.shift(12) - 1) * 100
    Y["usd"] = (Xl.usd / Xl.usd.shift(12) - 1) * 100
    Y["cvix"] = (Xl.cvix / Xl.cvix.shift(12) - 1) * 100
    Y["rate"] = Xl.rate - Xl.rate.shift(12)
    Y["move"] = (Xl.move / Xl.move.shift(12) - 1) * 100
    Y["gdp"] = Xl.gdp
    Y["cpi"] = Xl.cpi
    # unemployment: weighted average of country YoY pp changes (robust to composition)
    uy = (unm - unm.shift(12)).reindex(idx)
    Y["unemp"] = wavg(uy, W_LF)
    # USD: NEER ends a month or two before markets; extend with Fed broad dollar index growth, flagged
    usd_ext = None
    if Xl.usd.last_valid_index() < end:
        db = al(ME(fred("DTWEXBGS")))
        lv = Xl.usd.last_valid_index()
        ext = Xl.usd.copy()
        for t in idx[idx > lv]:
            ext[t] = ext[lv] * db[t] / db[lv]
        Y["usd"] = (ext / ext.shift(12) - 1) * 100
        usd_ext = f"{lv:%Y-%m}"
    liq_y = (liq / liq.shift(12) - 1) * 100
    meta = dict(end=end, liq_end=liq.index[-1], carried=carried, usd_ext=usd_ext,
                country_last={k: {c: (str(v.last_valid_index().strftime('%Y-%m')) if v.last_valid_index() is not None else None) for c, v in df.items()}
                              for k, df in {"gdp": pd.DataFrame(gq), "cpi": pd.DataFrame(cpi), "unemp": pd.DataFrame(un), "rate": yl_raw}.items()})
    return dict(C=C, G=G, liq=liq, liq_y=liq_y, Y=Y, Xl=Xl, cvix=X["cvix"], evz=evz, meta=meta)


# ---------------------------------------------------------------- model ----------------------------------------------------
def lead_corr(x, g, L, end_g, win=120, minn=120):
    """corr(x_t, g_{t+L}) over the last `win` pairs with t+L <= end_g."""
    g = g.reindex(x.index)
    xs = x.loc[:end_g - pd.DateOffset(months=L)]
    gs = g.shift(-L).reindex(xs.index)
    p = pd.concat([xs, gs], axis=1).dropna().iloc[-win:]
    return p.iloc[:, 0].corr(p.iloc[:, 1]) if len(p) >= minn else np.nan


def zlast(x, T, win, minn):
    s = x.loc[:T].dropna().iloc[-win:]
    if len(s) < minn or s.index[-1] != T: return np.nan
    return (s.iloc[-1] - s.mean()) / s.std(ddof=0)


def snapshot(Y, g, T, Tg, win_c=120, min_c=120, min_z=120):
    rows = {}
    for k, *_ in IND:
        x = Y[k]
        c3 = lead_corr(x, g, 3, Tg, win_c, min_c); c6 = lead_corr(x, g, 6, Tg, win_c, min_c)
        zc = zlast(x, T, 36, 36); zs = zlast(x, T, 120, min_z)
        rows[k] = dict(latest=x.get(T, np.nan), c3=c3, c6=c6, cavg=(c3 + c6) / 2, zc=zc, zs=zs, adj=zs * (c3 + c6) / 2)
    df = pd.DataFrame(rows).T
    gz = zlast(g, Tg, 120, min_z)
    s7 = df.loc[SUM_KEYS, "adj"].sum(min_count=len(SUM_KEYS))
    return df, gz, s7, s7 - gz


def history(Y, g, end, start="2009-01-01", min_obs=60):
    """Pseudo-real-time signal: at month T, indicators through T, liquidity through T-1, trailing windows only."""
    out = []
    for T in pd.date_range(start, end, freq="MS"):
        Tg = T - pd.DateOffset(months=1)
        if Tg not in g.dropna().index: continue
        df, gz, s7, sig = snapshot(Y, g, T, Tg, 120, min_obs, min_obs)
        full = not np.isnan(lead_corr(Y["usd"], g, 6, Tg, 120, 120)) and not np.isnan(zlast(g, Tg, 120, 120))
        out.append(dict(T=T, s7=s7, gz=gz, sig=sig, full=full, **{f"adj_{k}": df.loc[k, "adj"] for k in df.index}))
    return pd.DataFrame(out).set_index("T")


def rolling_corr(Y, g, start="2010-01-01"):
    out = {}
    for k, *_ in IND:
        vals = {}
        for Tg in pd.date_range(start, g.dropna().index[-1], freq="MS"):
            c3 = lead_corr(Y[k], g, 3, Tg, 120, 84); c6 = lead_corr(Y[k], g, 6, Tg, 120, 84)
            vals[Tg] = (c3 + c6) / 2
        out[k] = pd.Series(vals)
    return pd.DataFrame(out)


def evaluate(H, g, horizon=6):
    """Does the signal at T anticipate the change in liquidity YoY from T-1 (last known) to T+horizon?"""
    gg = g.dropna()
    fwd = pd.Series({T: gg.get(T + pd.DateOffset(months=horizon), np.nan) - gg.get(T - pd.DateOffset(months=1), np.nan) for T in H.index})
    E = pd.concat([H[["sig", "s7", "gz", "full"]], fwd.rename("fwd")], axis=1).dropna(subset=["sig", "fwd"])
    def stats(col, sub):
        x, y = sub[col], sub["fwd"]
        return dict(n=len(sub), corr=float(x.corr(y)), hit=float(((x > 0) == (y > 0)).mean()))
    res = {}
    for name, sub in {"all": E, "full_window": E[E.full]}.items():
        res[name] = {"signal": stats("sig", sub), "indicators_only": stats("s7", sub),
                     "minus_liq_z_only": dict(n=len(sub), corr=float((-sub.gz).corr(sub.fwd)), hit=float(((-sub.gz > 0) == (sub.fwd > 0)).mean()))}
    bands = [(-9, -0.5), (-0.5, 0), (0, 0.5), (0.5, 9)]
    res["bands"] = [dict(lo=lo, hi=hi, n=int(((E.sig >= lo) & (E.sig < hi)).sum()),
                         mean_fwd=float(E.loc[(E.sig >= lo) & (E.sig < hi), "fwd"].mean()) if ((E.sig >= lo) & (E.sig < hi)).any() else None,
                         up_share=float((E.loc[(E.sig >= lo) & (E.sig < hi), "fwd"] > 0).mean()) if ((E.sig >= lo) & (E.sig < hi)).any() else None)
                    for lo, hi in bands]
    return E, res


# ======================================================================= payload
import datetime as dt, math


def r(v, n=3):
    if v is None: return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else round(f, n)


def ser(s, n=3):
    s = s.dropna()
    return dict(d=[t.strftime("%Y-%m") for t in s.index], v=[r(x, n) for x in s.values])


def band(y):
    if y is None or (isinstance(y, float) and math.isnan(y)): return "n/a"
    return ("Significant " if abs(y) >= 0.5 else "Modest ") + ("uptrend" if y >= 0 else "downtrend")


def snap_dict(df, gz, s7, sig, T, Tg):
    keys = list(df.index)
    return dict(T=T.strftime("%Y-%m"), Tg=Tg.strftime("%Y-%m"), gz=r(gz), s7=r(s7), sig=r(sig), band=band(sig),
                **{c: [r(df.loc[k, c]) for k in keys] for c in ["latest", "c3", "c6", "cavg", "zc", "zs", "adj"]})


def build(raw, status, P, log=print):
    g, Y = P["liq_y"], P["Y"]; T = P["meta"]["end"]; Tg = P["meta"]["liq_end"]
    keys = [k for k, *_ in IND]
    now = snap_dict(*snapshot(Y, g, T, Tg), T, Tg)
    past = {}
    for lbl, m in [("m3", 3), ("m12", 12)]:
        t = T - pd.DateOffset(months=m); tg = min(t - pd.DateOffset(months=1), Tg)
        past[lbl] = snap_dict(*snapshot(Y, g, t, tg), t, tg)
    gg = g.dropna()
    past["m12"]["liq_then"] = r(gg.get(pd.Timestamp(past["m12"]["Tg"] + "-01")), 2)

    # ---- history and backtest
    H = history(Y, g, T, "2009-01-01", 60).dropna(subset=["sig"])
    hist = dict(d=[t.strftime("%Y-%m") for t in H.index], sig=[r(x) for x in H.sig], s7=[r(x) for x in H.s7],
                mgz=[r(-x) for x in H.gz], full=[bool(x) for x in H.full])
    E6, _ = evaluate(H, g, 6)
    scatter = dict(d=[t.strftime("%Y-%m") for t in E6.index], sig=[r(x) for x in E6.sig], s7=[r(x) for x in E6.s7],
                   mgz=[r(-x) for x in E6.gz], fwd=[r(x, 2) for x in E6.fwd])
    bt = {}
    for h in [3, 6, 9, 12]:
        _, res = evaluate(H, g, h)
        bt[h] = {w: {k: dict(n=v["n"], corr=r(v["corr"]), hit=r(v["hit"])) for k, v in res[w].items()} for w in ["all", "full_window"]}
        if h == 6: bands6 = res["bands"]
    for b in bands6:
        for k in ("mean_fwd", "up_share"): b[k] = r(b[k])

    # ---- lead-lag profile on the current window
    def xc(x, L):
        gx = g.reindex(x.index); p = pd.concat([x, gx.shift(-L)], axis=1).dropna()
        p = p[p.index <= Tg - pd.DateOffset(months=max(L, 0))].iloc[-120:]
        return p.iloc[:, 0].corr(p.iloc[:, 1]) if len(p) >= 100 else np.nan
    lags = [-12, -6, -3, 0, 3, 6, 9, 12]
    prof = [[r(xc(Y[k], L), 2) for L in lags] for k in keys]

    # ---- rolling 10y average lead correlation
    R = rolling_corr(Y, g, "2012-01-01")
    roll = dict(d=[t.strftime("%Y-%m") for t in R.index], **{k: [r(x) for x in R[k]] for k in keys})

    # ---- liquidity proxy
    G, C = P["G"] / 1e3, P["C"] / 1e3
    liq = dict(d=[t.strftime("%Y-%m") for t in G.index], groups={c: [r(x, 3) for x in G[c]] for c in G.columns},
               level=r(P["liq"].iloc[-1] / 1e3, 2),
               comps=[dict(name=c, tn=r(C[c].iloc[-1], 2), yoy=r((C[c].iloc[-1] / C[c].iloc[-13] - 1) * 100, 1)) for c in C.columns])

    ind = {k: ser(Y[k].loc["2004-01-01":], 3) for k in keys}

    meta = dict(built=dt.datetime.now(dt.timezone.utc).strftime("%d %b %Y %H:%M UTC"), T=T.strftime("%b %Y"), Tg=Tg.strftime("%b %Y"),
                carried={k: v for k, v in P["meta"]["carried"].items() if pd.Period(v, "M") < Tg.to_period("M")},
                usd_ext=P["meta"]["usd_ext"], country_last=P["meta"]["country_last"], status=status,
                hist_full_from=H[H.full].index[0].strftime("%b %Y"), hist_from=H.index[0].strftime("%b %Y"))
    names = [n for _, n, _, _ in IND]; proxies = [p for _, _, p, _ in IND]; tf = [t for *_, t in IND]
    return dict(meta=meta, keys=keys, names=names, proxies=proxies, tf=tf, sum_keys=SUM_KEYS, now=now, past=past,
                hist=hist, scatter=scatter, bt=bt, bands6=bands6, lags=lags, prof=prof, roll=roll, liq=liq, ind=ind,
                liq_yoy=ser(g, 2))




TEMPLATE = r"""<title>Global Liquidity Lead Indicator Model</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&family=Space+Grotesk:wght@500;600;700&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
/* Layout: research-note column. Verdict band first, findings, then paired chart cards; tables scroll inside their cards. */
:root{
  --paper:#F7F1E6; --card:#FDFAF3; --ink:#221C14; --mut:#6E6556; --rule:#E3D9C6; --grid:#ECE4D4;
  --pos:#0E756C; --neg:#D2622A; --short:#A67A22; --long:#4E6577; --plum:#7A4A66;
  --pos-soft:rgba(14,117,108,.10); --neg-soft:rgba(210,98,42,.10); --shade:rgba(78,101,119,.08);
  --f-head:'Space Grotesk',ui-sans-serif,system-ui,sans-serif;
  --f-body:'IBM Plex Sans',ui-sans-serif,system-ui,-apple-system,'Segoe UI',sans-serif;
  --f-mono:'IBM Plex Mono',ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --paper:#1B1812; --card:#24201A; --ink:#EEE7D9; --mut:#A99F8D; --rule:#3A342A; --grid:#2F2A22;
  --pos:#2A9E90; --neg:#D9703C; --short:#B88A35; --long:#7F98AB; --plum:#A06D92;
  --pos-soft:rgba(42,158,144,.14); --neg-soft:rgba(217,112,60,.14); --shade:rgba(127,152,171,.10); color-scheme:dark}}
:root[data-theme="dark"]{
  --paper:#1B1812; --card:#24201A; --ink:#EEE7D9; --mut:#A99F8D; --rule:#3A342A; --grid:#2F2A22;
  --pos:#2A9E90; --neg:#D9703C; --short:#B88A35; --long:#7F98AB; --plum:#A06D92;
  --pos-soft:rgba(42,158,144,.14); --neg-soft:rgba(217,112,60,.14); --shade:rgba(127,152,171,.10); color-scheme:dark}
*{box-sizing:border-box}
body{background:var(--paper);color:var(--ink);font:15px/1.55 var(--f-body);-webkit-font-smoothing:antialiased}
.wrap{max-width:1200px;margin:0 auto;padding-block:28px 56px;padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px));display:grid;gap:28px}
h1,h2,h3{font-family:var(--f-head);text-wrap:balance;margin:0;letter-spacing:-.01em}
h1{font-size:clamp(26px,3.6vw,36px);font-weight:700;line-height:1.1}
h2{font-size:21px;font-weight:600}
h3{font-size:15px;font-weight:600}
p{margin:0}
.lede{color:var(--mut);max-width:68ch;margin-top:8px}
.asof{font:13px var(--f-mono);color:var(--mut);margin-top:10px;display:flex;flex-wrap:wrap;gap:4px 18px}
.num{font-family:var(--f-mono);font-variant-numeric:tabular-nums}
.card{background:var(--card);border:1px solid var(--rule);border-radius:6px;padding:18px 18px 16px;min-width:0}
.sec{display:grid;gap:14px}
.sec-head{display:flex;flex-wrap:wrap;align-items:end;justify-content:space-between;gap:10px 20px}
.sec-head p{color:var(--mut);max-width:72ch;font-size:14px;margin-top:4px}
.grid2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
@media (max-width:860px){.grid2{grid-template-columns:minmax(0,1fr)}}
.chart{position:relative;height:330px}
.chart.tall{height:380px}.chart.short{height:240px}.chart.mini{height:132px}
.cap{font-size:13px;color:var(--mut);margin-top:8px}
.card h3+.cap{margin-top:2px;margin-bottom:10px}

/* verdict */
.verdict{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,1fr);gap:14px}
@media (max-width:860px){.verdict{grid-template-columns:minmax(0,1fr)}}
.call{display:grid;grid-template-columns:auto minmax(0,1fr);gap:6px 22px;align-items:center}
@media (max-width:520px){.call{grid-template-columns:minmax(0,1fr)}}
.call .big{font:500 clamp(52px,8vw,76px)/1 var(--f-mono);letter-spacing:-.03em;font-variant-numeric:tabular-nums}
.pill{display:inline-flex;align-items:center;gap:7px;font:600 13px var(--f-body);padding:4px 10px 4px 8px;border-radius:999px;border:1px solid currentColor}
.pill svg{width:12px;height:12px}
.call .why{font-size:14px;color:var(--mut);grid-column:1/-1;max-width:62ch}
.spark{grid-column:1/-1;height:96px;position:relative;margin-top:6px}
.tiles{display:grid;grid-template-columns:minmax(0,1fr);gap:0}
.tile{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:2px 14px;padding:11px 0;border-bottom:1px solid var(--rule);align-items:baseline}
.tile:first-child{padding-top:0}.tile:last-child{border-bottom:0;padding-bottom:0}
.tile .k{font-size:13px;color:var(--mut)}
.tile .v{font:500 20px var(--f-mono);font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}
.tile .s{grid-column:1/-1;font-size:13px;color:var(--mut)}

/* findings */
.findings{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px 28px}
@media (max-width:860px){.findings{grid-template-columns:minmax(0,1fr)}}
.finding{border-top:2px solid var(--ink);padding-top:10px}
.finding h3{margin-bottom:5px}
.finding p{font-size:14px;max-width:62ch}

/* controls */
.seg{display:inline-flex;flex-wrap:wrap;border:1px solid var(--rule);border-radius:6px;overflow:hidden;background:var(--card)}
.seg button{font:500 13px var(--f-body);color:var(--ink);background:transparent;border:0;border-right:1px solid var(--rule);padding:7px 12px;cursor:pointer}
.seg button:last-child{border-right:0}
.seg button[aria-pressed="true"]{background:var(--ink);color:var(--card)}
.seg button:hover:not([aria-pressed="true"]){background:var(--grid)}
button:focus-visible,select:focus-visible,input:focus-visible{outline:2px solid var(--neg);outline-offset:2px}
.ctrl{display:flex;flex-wrap:wrap;gap:10px 16px;align-items:center;font-size:13px;color:var(--mut)}
select{font:500 13px var(--f-body);color:var(--ink);background:var(--card);border:1px solid var(--rule);border-radius:6px;padding:6px 8px}
label.chk{display:inline-flex;gap:6px;align-items:center;cursor:pointer}
.legend{display:flex;flex-wrap:wrap;gap:4px 16px;font-size:12.5px;color:var(--mut);margin-bottom:8px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}
.legend i.line{height:2px;border-radius:0;vertical-align:3px;width:14px}

/* tables */
.tscroll{overflow-x:auto;margin:0 -2px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:7px 9px;text-align:right;border-bottom:1px solid var(--rule);white-space:nowrap}
th{font:500 12px var(--f-body);color:var(--mut);vertical-align:bottom}
td{font-family:var(--f-mono);font-variant-numeric:tabular-nums}
th:first-child,td:first-child{text-align:left;font-family:var(--f-body);position:sticky;left:0;background:var(--card)}
td.txt{font-family:var(--f-body);text-align:left;color:var(--mut);white-space:normal;min-width:220px}
tr.sep td{border-top:2px solid var(--ink)}
td.best{font-weight:600}
.heat td{text-align:center;color:var(--ink)}
.heat td.peak{outline:2px solid var(--ink);outline-offset:-2px}
.neg{color:var(--neg)}.pos{color:var(--pos)}
.muted{color:var(--mut)}

/* small multiples */
.mult{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px 18px}
@media (max-width:860px){.mult{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:520px){.mult{grid-template-columns:minmax(0,1fr)}}
.m h4{font:500 13px var(--f-body);margin:0;display:flex;justify-content:space-between;gap:8px}
.m h4 span{font-family:var(--f-mono);color:var(--mut)}

/* notes */
.notes{columns:2 340px;column-gap:32px;font-size:13.5px}
.notes>div{break-inside:avoid;margin-bottom:16px}
.notes h3{font-size:14px;margin-bottom:4px}
.notes p,.notes li{color:var(--mut);max-width:66ch}
.notes ul{margin:4px 0 0;padding-left:18px}
footer{font-size:12.5px;color:var(--mut);border-top:1px solid var(--rule);padding-top:14px}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
</style>

<div class="wrap">
  <header>
    <h1>Global Liquidity Lead Indicator Model</h1>
    <p class="lede">A leading-indicator model for global liquidity growth, built on public data. Nine market and macro indicators are weighted by how well they have led liquidity growth over the past ten years. Their sum, less liquidity's own z-score, signals whether liquidity growth should rise or fall over the medium term.</p>
    <div class="asof" id="asof"></div>
  </header>

  <section class="verdict" aria-label="Current signal">
    <div class="card call" id="call"></div>
    <div class="card tiles" id="tiles"></div>
  </section>

  <section class="sec" aria-labelledby="h-find">
    <h2 id="h-find">What the model shows</h2>
    <div class="findings" id="findings"></div>
  </section>

  <section class="sec" aria-labelledby="h-rep">
    <div class="sec-head">
      <div><h2 id="h-rep">Indicator breakdown</h2>
        <p>Correlation-adjusted z = structural (10y) z-score × the average of the 3- and 6-month lead correlations. Compare today's reading with three and twelve months ago.</p></div>
      <div class="seg" role="group" aria-label="Reading date" id="modeSeg">
        <button type="button" data-mode="now" aria-pressed="true">Now</button>
        <button type="button" data-mode="m3" aria-pressed="false">3 months ago</button>
        <button type="button" data-mode="m12" aria-pressed="false">12 months ago</button>
      </div>
    </div>
    <div class="card">
      <h3>Correlation-adjusted z-score (t10y)</h3>
      <p class="cap" id="adjCap"></p>
      <div class="legend"><span><i style="background:var(--pos)"></i>Points to faster liquidity growth</span><span><i style="background:var(--neg)"></i>Points to slower growth</span><span><i style="background:var(--ink)"></i>Signal: sum ex stocks &amp; crypto minus liquidity YoY z</span></div>
      <div class="chart tall"><canvas id="cAdj" role="img" aria-label="Correlation-adjusted z-scores by indicator"></canvas></div>
    </div>
    <div class="grid2">
      <div class="card">
        <h3>Trailing 10y correlation with liquidity YoY, by lead</h3>
        <p class="cap">Indicator YoY at month t against liquidity YoY at t+3 and t+6.</p>
        <div class="legend"><span><i style="background:var(--short)"></i>3-month lead</span><span><i style="background:var(--long)"></i>6-month lead</span></div>
        <div class="chart"><canvas id="cCorr" role="img" aria-label="Lead correlations"></canvas></div>
      </div>
      <div class="card">
        <h3>Z-score of the latest YoY reading</h3>
        <p class="cap">Against the trailing 3 years (cyclical) and 10 years (structural).</p>
        <div class="legend"><span><i style="background:var(--short)"></i>Cyclical, t3y</span><span><i style="background:var(--long)"></i>Structural, t10y</span></div>
        <div class="chart"><canvas id="cZ" role="img" aria-label="Z-scores"></canvas></div>
      </div>
    </div>
  </section>

  <section class="sec" aria-labelledby="h-hist">
    <div class="sec-head"><div><h2 id="h-hist">Signal history</h2>
      <p id="histCap"></p></div></div>
    <div class="card">
      <h3>Signal and its two parts, z units</h3>
      <div class="legend"><span><i class="line" style="background:var(--ink)"></i>Signal</span><span><i class="line" style="background:var(--pos)"></i>Sum of 7 adjusted indicators</span><span><i class="line" style="background:var(--plum)"></i>Minus liquidity YoY z</span><span><i style="background:var(--shade)"></i>5–10y windows (history too short for full 10y)</span></div>
      <div class="chart tall"><canvas id="cHist" role="img" aria-label="Signal history"></canvas></div>
    </div>
    <div class="card">
      <h3>Global liquidity proxy, YoY %</h3>
      <div class="chart short"><canvas id="cLiqY" role="img" aria-label="Liquidity year on year growth"></canvas></div>
    </div>
  </section>

  <section class="sec" aria-labelledby="h-bt">
    <div class="sec-head"><div><h2 id="h-bt">Does it forecast?</h2>
      <p>Each month's signal against the change in liquidity YoY from the last observed month to six months ahead. Weights and z-scores use only data available at the time; data revisions and release lags beyond one month are ignored.</p></div>
      <div class="seg" role="group" aria-label="Predictor" id="predSeg">
        <button type="button" data-p="sig" aria-pressed="true">Full signal</button>
        <button type="button" data-p="s7" aria-pressed="false">Indicators only</button>
        <button type="button" data-p="mgz" aria-pressed="false">Minus liquidity z only</button>
      </div></div>
    <div class="grid2">
      <div class="card">
        <h3 id="scTitle">Signal vs next-6-month change in liquidity YoY</h3>
        <p class="cap" id="scCap"></p>
        <div class="chart"><canvas id="cScat" role="img" aria-label="Scatter of signal against forward change"></canvas></div>
      </div>
      <div class="card">
        <h3>Correlation and hit rate by horizon</h3>
        <p class="cap">Hit rate = share of months where the predictor and the subsequent change have the same sign.</p>
        <div class="tscroll"><table id="btTable"></table></div>
        <h3 style="margin-top:18px">Six months after each signal band</h3>
        <div class="tscroll"><table id="bandTable"></table></div>
      </div>
    </div>
  </section>

  <section class="sec" aria-labelledby="h-lead">
    <div class="sec-head"><div><h2 id="h-lead">Do the indicators actually lead?</h2>
      <p>Correlation of each indicator's YoY with liquidity YoY, over the latest ten years, at leads from −12 months (indicator lags liquidity) to +12 (indicator leads). Outlined cell = strongest correlation.</p></div></div>
    <div class="card"><div class="tscroll"><table class="heat" id="heat"></table></div></div>
    <div class="card">
      <h3>Rolling 10y lead correlation, average of 3m and 6m</h3>
      <p class="cap">The weights the model applies, month by month. Windows shorter than 10 years (min. 7) before 2014.</p>
      <div class="mult" id="mult"></div>
    </div>
  </section>

  <section class="sec" aria-labelledby="h-liq">
    <div class="sec-head"><div><h2 id="h-liq">The liquidity proxy</h2>
      <p>Global liquidity is measured as central bank balance sheets + broad money + FX reserves ex gold, for the economies with timely public data, in USD.</p></div></div>
    <div class="grid2">
      <div class="card">
        <h3>Composition, USD trillions</h3>
        <div class="legend"><span><i style="background:var(--long)"></i>Central bank assets</span><span><i style="background:var(--pos)"></i>Broad money</span><span><i style="background:var(--short)"></i>FX reserves ex gold</span></div>
        <div class="chart"><canvas id="cLiq" role="img" aria-label="Liquidity proxy composition"></canvas></div>
      </div>
      <div class="card">
        <h3 id="compTitle">Components</h3>
        <div class="tscroll"><table id="compTable"></table></div>
      </div>
    </div>
  </section>

  <section class="sec" aria-labelledby="h-exp">
    <div class="sec-head"><div><h2 id="h-exp">Indicator explorer</h2>
      <p>Each series z-scored over 2004 onwards. Shift the indicator forward by its lead to see whether its turns precede liquidity's.</p></div>
      <div class="ctrl">
        <label for="expInd">Indicator</label><select id="expInd"></select>
        <label for="expLead">Lead</label><select id="expLead"><option value="0">0m</option><option value="3">3m</option><option value="6" selected>6m</option><option value="9">9m</option><option value="12">12m</option></select>
        <label class="chk"><input type="checkbox" id="expInv">Invert indicator</label>
      </div></div>
    <div class="card">
      <h3 id="expTitle"></h3>
      <p class="cap" id="expCap"></p>
      <div class="legend"><span><i class="line" style="background:var(--ink)"></i>Liquidity YoY, z</span><span><i class="line" style="background:var(--pos)"></i><span id="expLeg">Indicator YoY, z, shifted forward</span></span></div>
      <div class="chart"><canvas id="cExp" role="img" aria-label="Indicator vs liquidity"></canvas></div>
    </div>
  </section>

  <section class="sec" aria-labelledby="h-tab">
    <h2 id="h-tab">Indicator detail</h2>
    <div class="card"><div class="tscroll"><table id="indTable"></table></div></div>
  </section>

  <section class="sec" aria-labelledby="h-notes">
    <h2 id="h-notes">Method and data</h2>
    <div class="card notes" id="notes"></div>
  </section>
  <footer id="foot"></footer>
</div>

<script>
const D=/*DATA*/null/*END*/;
(function(){
"use strict";
const $=s=>document.querySelector(s);
const K=D.keys, N=D.names, S7=D.sum_keys;
const SN={stock:"Stocks",crypto:"Crypto",usd:"USD NEER",cvix:"CVIX",rate:"Global 10y yields",move:"MOVE",gdp:"Global real GDP",cpi:"Global core CPI",unemp:"Unemployment"};
const NS=K.map(k=>SN[k]);
const isNum=v=>typeof v==="number"&&isFinite(v);
const MINUS="−";
const f=(v,d=2,sign=false)=>{if(!isNum(v))return"n/a";const s=Math.abs(v).toFixed(d);return(v<0?MINUS:(sign&&v>0?"+":""))+s};
const fu=(v,i)=>{if(!isNum(v))return"n/a";return D.tf[i]==="diff"?f(v,2,true)+" pp":f(v,1,true)+"%"};
const MON=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
const ym=s=>{const[y,m]=s.split("-");return MON[+m-1]+" "+y};
const css=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const hexA=(hex,a)=>{const h=hex.replace("#","");const n=parseInt(h.length===3?h.split("").map(x=>x+x).join(""):h,16);return`rgba(${n>>16&255},${n>>8&255},${n&255},${a})`};
let P={}; const charts={};
function pal(){P={ink:css("--ink"),mut:css("--mut"),grid:css("--grid"),rule:css("--rule"),card:css("--card"),pos:css("--pos"),neg:css("--neg"),
  short:css("--short"),long:css("--long"),plum:css("--plum"),shade:css("--shade"),posS:css("--pos-soft"),negS:css("--neg-soft")};}
function mk(id,cfg){if(charts[id])charts[id].destroy();charts[id]=new Chart(document.getElementById(id),cfg);return charts[id]}
function tip(cb){return{backgroundColor:P.card,titleColor:P.ink,bodyColor:P.ink,footerColor:P.ink,borderColor:P.rule,borderWidth:1,padding:10,
  titleFont:{family:"IBM Plex Sans",weight:"600"},bodyFont:{family:"IBM Plex Mono"},boxPadding:4,callbacks:cb||{}}}
function opts(extra){return Object.assign({responsive:true,maintainAspectRatio:false,animation:false},extra)}
const tick=()=>({color:P.mut,font:{family:"IBM Plex Mono",size:11}});
const tickS=()=>({color:P.ink,font:{family:"IBM Plex Sans",size:12}});

const now=D.now, p3=D.past.m3, p12=D.past.m12, M=D.meta;
const liqNow=D.liq_yoy.v.at(-1);
const b6=D.bt[6].all;

/* ---------- header, verdict, tiles, findings ---------- */
const nStale=(M.stale||[]).length;
$("#asof").innerHTML=`<span>Indicators to ${M.T}</span><span>Liquidity proxy to ${M.Tg}</span><span>Built ${M.built}</span>${nStale?`<span class="neg">${nStale} source${nStale>1?"s":""} from last good download (see Method and data)</span>`:""}`;
const dirIcon=up=>up?'<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 1.5 11 9H1z" fill="currentColor"/></svg>':'<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 10.5 1 3h10z" fill="currentColor"/></svg>';
function drivers(){
  const idx=S7.map(k=>K.indexOf(k)).filter(i=>isNum(now.adj[i])).sort((a,b)=>Math.abs(now.adj[b])-Math.abs(now.adj[a]));
  return idx.slice(0,2);
}
function drawCall(){
  const up=now.sig>=0, col=up?"var(--pos)":"var(--neg)";
  const [a,b]=drivers();
  const dir=i=>(now.adj[i]>=0?"faster":"slower");
  $("#call").innerHTML=`<div class="big" style="color:${col}">${f(now.sig,2,true)}</div>
   <div><span class="pill" style="color:${col}">${dirIcon(up)}${now.band}</span>
   <p class="cap" style="margin-top:8px">Sum of seven correlation-adjusted indicators <span class="num">${f(now.s7,2)}</span>, minus liquidity YoY z-score <span class="num">${f(now.gz,2)}</span>. Below 0.5 in either direction is modest; 0.5 and above is significant.</p></div>
   <p class="why">Largest contributions: ${NS[a]} (<span class="num">${fu(now.latest[a],a)}</span> YoY, adjusted z <span class="num">${f(now.adj[a],2)}</span>) and ${NS[b]} (<span class="num">${fu(now.latest[b],b)}</span>, <span class="num">${f(now.adj[b],2)}</span>), both pointing to ${dir(a)===dir(b)?dir(a):dir(a)+" and "+dir(b)+" respectively"} liquidity growth.</p>
   <div class="spark"><canvas id="cSpark" role="img" aria-label="Signal over the last two years"></canvas></div>`;
}
function drawTiles(){
  $("#tiles").innerHTML=`
  <div class="tile"><span class="k">Signal 3 months ago, ${ym(p3.T)}</span><span class="v">${f(p3.sig,2,true)}</span>
    <span class="s">${p3.band}.</span></div>
  <div class="tile"><span class="k">Signal 12 months ago, ${ym(p12.T)}</span><span class="v">${f(p12.sig,2,true)}</span>
    <span class="s">${p12.band}. Liquidity YoY has since moved from <span class="num">${f(p12.liq_then,1)}%</span> (${ym(p12.Tg)}) to <span class="num">${f(liqNow,1)}%</span>.</span></div>
  <div class="tile"><span class="k">Global liquidity proxy, ${M.Tg}</span><span class="v">$${D.liq.level.toFixed(1)}tn</span>
    <span class="s"><span class="num">${f(liqNow,1,true)}%</span> YoY; structural z-score <span class="num">${f(now.gz,2)}</span>.</span></div>
  <div class="tile"><span class="k">Correlation with next-6m liquidity change, since ${M.hist_from}</span><span class="v">${b6.signal.corr.toFixed(2)}</span>
    <span class="s">Indicators alone <span class="num">${b6.indicators_only.corr.toFixed(2)}</span>; minus liquidity z alone <span class="num">${b6.minus_liq_z_only.corr.toFixed(2)}</span>.</span></div>`;
}
function drawFindings(){
  const [a,b]=drivers();
  const share=(Math.abs(now.adj[a])+Math.abs(now.adj[b]))/S7.map(k=>Math.abs(now.adj[K.indexOf(k)])||0).reduce((x,y)=>x+y,0);
  const flipped=Math.sign(p12.sig)!==Math.sign(now.sig);
  const liqUp=liqNow>p12.liq_then, callUp=p12.sig>=0;
  const gzNear=Math.abs(now.gz)<0.3;
  const items=[
   ["What drives today's reading",`${NS[a]} and ${NS[b]} account for ${Math.round(share*100)}% of the absolute weight in the seven-indicator sum. ${NS[a]} is ${fu(now.latest[a],a)} YoY (structural z ${f(now.zs[a],2)}) with a lead correlation of ${f(now.cavg[a],2)}; ${NS[b]} is ${fu(now.latest[b],b)} (z ${f(now.zs[b],2)}, correlation ${f(now.cavg[b],2)}). Liquidity YoY sits ${f(now.gz,2)} standard deviations from its ten-year mean, so ${gzNear?"it adds little to the signal":"it moves the signal by "+f(-now.gz,2)}.`],
   [flipped?"The signal has flipped":"The signal has held its direction",`Twelve months ago the model read ${f(p12.sig,2,true)} (${p12.band.toLowerCase()}); three months ago ${f(p3.sig,2,true)}; now ${f(now.sig,2,true)}. Over the twelve months liquidity YoY went from ${f(p12.liq_then,1)}% to ${f(liqNow,1)}%, ${liqUp===callUp?"in line with that earlier call":"against that earlier call"}.`],
   ["Mean reversion does most of the forecasting",`Since ${M.hist_from}, liquidity's own z-score (sign flipped) tracks the next six months' change better (r ${b6.minus_liq_z_only.corr.toFixed(2)}) than the full signal (${b6.signal.corr.toFixed(2)}). The seven-indicator sum adds almost nothing (${b6.indicators_only.corr.toFixed(2)}, hit rate ${Math.round(b6.indicators_only.hit*100)}%).${gzNear?" With liquidity's z-score near zero, today's call rests almost entirely on the indicator sum, the part with the weaker record.":""}`],
   ["Several leads are really coincident",`Stocks, crypto and the dollar correlate most strongly with liquidity at zero lag or when they trail it, and part of the dollar link is mechanical because the proxy is measured in USD. Yields, unemployment and GDP do lead by 6–12 months, but their correlations changed sign between 2017 and 2021, so the weights depend heavily on the window.`]
  ];
  $("#findings").innerHTML=items.map(([h,p])=>`<div class="finding"><h3>${h}</h3><p>${p}</p></div>`).join("");
}

/* ---------- breakdown charts ---------- */
let mode="now";
const snapFor=m=>m==="m3"?p3:m==="m12"?p12:now;
function hbar(id,labels,sets,xmin,xmax){
  return mk(id,{type:"bar",data:{labels,datasets:sets},options:opts({indexAxis:"y",layout:{padding:{left:6}},
    interaction:{mode:"index",axis:"y",intersect:false},
    plugins:{legend:{display:false},tooltip:tip({label:c=>` ${c.dataset.label}: ${isNum(c.raw)?f(c.raw,2):"n/a"}`})},
    scales:{x:{min:xmin,max:xmax,grid:{color:P.grid},border:{display:false},ticks:Object.assign(tick(),{callback:v=>f(v,1)})},
            y:{grid:{display:false},border:{color:P.rule},ticks:tickS()}}})});
}
const barStyle={borderRadius:4,borderSkipped:"start",barPercentage:.86,categoryPercentage:.8};
function drawBreakdown(){
  const s=snapFor(mode), lbl=mode==="now"?`Now, ${ym(s.T)}`:`${mode==="m3"?"3":"12"} months ago, ${ym(s.T)}`;
  const vals=[...s.adj,s.sig];
  const cols=vals.map((v,i)=>i===vals.length-1?P.ink:(isNum(v)&&v>=0?P.pos:P.neg));
  const lim=Math.max(1.2,Math.ceil(Math.max(...vals.filter(isNum).map(Math.abs))*4+0.5)/4);
  hbar("cAdj",[...NS,"Signal"],[Object.assign({label:lbl,data:vals,backgroundColor:cols},barStyle)],-lim,lim);
  const miss=K.filter((k,i)=>!isNum(s.adj[i])).map(k=>SN[k]);
  $("#adjCap").textContent=`${lbl}. Signal ${f(s.sig,2,true)}: ${s.band.toLowerCase()}. Sum of seven ${f(s.s7,2)}, liquidity YoY z ${f(s.gz,2)}.${miss.length?` ${miss.join(", ")}: under ten years of history at this date, so no weight (excluded from the sum either way).`:""}`;
  hbar("cCorr",NS,[Object.assign({label:"3-month lead",data:s.c3,backgroundColor:P.short},barStyle),Object.assign({label:"6-month lead",data:s.c6,backgroundColor:P.long},barStyle)],-0.8,0.8);
  const zl=Math.max(2.5,Math.ceil(Math.max(...[...s.zc,...s.zs].filter(isNum).map(Math.abs))*2)/2);
  hbar("cZ",NS,[Object.assign({label:"Cyclical, t3y",data:s.zc,backgroundColor:P.short},barStyle),Object.assign({label:"Structural, t10y",data:s.zs,backgroundColor:P.long},barStyle)],-zl,zl);
}
$("#modeSeg").addEventListener("click",e=>{const b=e.target.closest("button");if(!b)return;mode=b.dataset.mode;
  document.querySelectorAll("#modeSeg button").forEach(x=>x.setAttribute("aria-pressed",String(x===b)));drawBreakdown();});

/* ---------- history ---------- */
const H=D.hist;
const fullIdx=H.full.indexOf(true);
const bandsPlugin={id:"bands",beforeDatasetsDraw(c,_,o){const{ctx,chartArea:a,scales:{x,y}}=c;if(!y||!o)return;ctx.save();
  if(o.bands){const y5=y.getPixelForValue(0.5),yn=y.getPixelForValue(-0.5);
    ctx.fillStyle=P.posS;ctx.fillRect(a.left,a.top,a.right-a.left,Math.max(0,Math.min(y5,a.bottom)-a.top));
    ctx.fillStyle=P.negS;const t=Math.max(yn,a.top);ctx.fillRect(a.left,t,a.right-a.left,Math.max(0,a.bottom-t));}
  if(o.shadeTo>0){const xs=x.getPixelForValue(o.shadeTo);ctx.fillStyle=P.shade;ctx.fillRect(a.left,a.top,xs-a.left,a.bottom-a.top);}
  ctx.restore();}};
Chart.register(bandsPlugin);
function line(label,data,color,w,extra){return Object.assign({label,data,borderColor:color,backgroundColor:color,borderWidth:w,pointRadius:0,pointHoverRadius:4,pointHoverBorderWidth:2,pointHoverBorderColor:P.card,tension:0,spanGaps:false},extra||{})}
function timeX(labels,every){return{grid:{display:false},border:{color:P.rule},ticks:Object.assign(tick(),{autoSkip:false,maxRotation:0,callback:(v,i)=>{const s=labels[i];return s&&s.endsWith("-01")&&(+s.slice(0,4))%(every||2)===0?s.slice(0,4):null}})}}
function drawHistory(){
  $("#histCap").textContent=`Rebuilt month by month from ${M.hist_from} using only trailing data. Shaded bands mark the significant zones (±0.5). Before ${M.hist_full_from} the correlation and z-score windows are shorter than ten years.`;
  mk("cHist",{type:"line",data:{labels:H.d,datasets:[line("Signal",H.sig,P.ink,2),line("Sum of 7 adjusted",H.s7,P.pos,1.4),line("Minus liquidity YoY z",H.mgz,P.plum,1.4)]},
    options:opts({interaction:{mode:"index",intersect:false},
      plugins:{legend:{display:false},bands:{bands:true,shadeTo:fullIdx},tooltip:tip({title:i=>ym(H.d[i[0].dataIndex]),label:c=>` ${c.dataset.label}: ${f(c.raw,2)}`})},
      scales:{x:timeX(H.d,2),y:{grid:{color:P.grid},border:{display:false},ticks:Object.assign(tick(),{callback:v=>f(v,1)})}}})});
  const G=D.liq_yoy, gi=Math.max(0,G.d.indexOf(H.d[0]));
  const gd=G.d.slice(gi), gv=G.v.slice(gi);
  mk("cLiqY",{type:"line",data:{labels:gd,datasets:[line("Liquidity YoY",gv,P.long,2,{fill:{target:"origin",above:hexA(P.long,.12),below:hexA(P.neg,.12)}})]},
    options:opts({interaction:{mode:"index",intersect:false},
      plugins:{legend:{display:false},tooltip:tip({title:i=>ym(gd[i[0].dataIndex]),label:c=>` Liquidity YoY: ${f(c.raw,1,true)}%`})},
      scales:{x:timeX(gd,2),y:{grid:{color:P.grid},border:{display:false},ticks:Object.assign(tick(),{callback:v=>v+"%"})}}})});
  const n=24, sd=H.d.slice(-n), sv=H.sig.slice(-n);
  mk("cSpark",{type:"line",data:{labels:sd,datasets:[line("Signal",sv,P.ink,2,{pointRadius:sv.map((_,i)=>i===sv.length-1?3.5:0),pointBackgroundColor:now.sig>=0?P.pos:P.neg,pointBorderColor:P.card,pointBorderWidth:2})]},
    options:opts({layout:{padding:{top:4,right:26}},interaction:{mode:"index",intersect:false},
      plugins:{legend:{display:false},bands:{bands:true},tooltip:tip({title:i=>ym(sd[i[0].dataIndex]),label:c=>` Signal: ${f(c.raw,2)}`})},
      scales:{x:{grid:{display:false},border:{display:false},ticks:Object.assign(tick(),{maxRotation:0,autoSkip:false,callback:(v,i)=>(sd[i].endsWith("-01")||i===sd.length-1)?ym(sd[i]).replace(" "," "):null})},
              y:{grid:{color:P.grid},border:{display:false},ticks:Object.assign(tick(),{maxTicksLimit:3,callback:v=>f(v,1)})}}})});
}

/* ---------- backtest ---------- */
let pred="sig";
const predName={sig:"Signal",s7:"Sum of 7 adjusted indicators",mgz:"Minus liquidity YoY z"};
const predKey={sig:"signal",s7:"indicators_only",mgz:"minus_liq_z_only"};
function ols(x,y){const n=x.length,mx=x.reduce((a,b)=>a+b,0)/n,my=y.reduce((a,b)=>a+b,0)/n;let sxy=0,sxx=0,syy=0;
  for(let i=0;i<n;i++){sxy+=(x[i]-mx)*(y[i]-my);sxx+=(x[i]-mx)**2;syy+=(y[i]-my)**2}const b=sxy/sxx;return{a:my-b*mx,b,r:sxy/Math.sqrt(sxx*syy)}}
function drawScatter(){
  const S=D.scatter, xs=[],ys=[],pts=[];
  S.d.forEach((d,i)=>{const x=S[pred][i],y=S.fwd[i];if(isNum(x)&&isNum(y)){xs.push(x);ys.push(y);pts.push({x,y,d})}});
  const o=ols(xs,ys), xmin=Math.min(...xs), xmax=Math.max(...xs);
  const nowX=pred==="sig"?now.sig:pred==="s7"?now.s7:-now.gz;
  $("#scTitle").textContent=`${predName[pred]} vs next-6-month change in liquidity YoY`;
  $("#scCap").innerHTML=`${pts.length} months, ${ym(pts[0].d)} to ${ym(pts.at(-1).d)}. r = <span class="num">${f(o.r,2)}</span>, hit rate <span class="num">${Math.round(b6[predKey[pred]].hit*100)}%</span>. Dashed line = today's reading (<span class="num">${f(nowX,2)}</span>). Overlapping six-month windows make the effective sample far smaller than the month count.`;
  const nowLine={id:"nowLine",afterDatasetsDraw(c){const{ctx,chartArea:a,scales:{x}}=c;const xp=x.getPixelForValue(nowX);if(xp<a.left||xp>a.right)return;ctx.save();ctx.strokeStyle=nowX>=0?P.pos:P.neg;ctx.setLineDash([4,3]);ctx.lineWidth=1.5;ctx.beginPath();ctx.moveTo(xp,a.top);ctx.lineTo(xp,a.bottom);ctx.stroke();ctx.restore();}};
  mk("cScat",{type:"scatter",data:{datasets:[
      {label:"Months",data:pts,backgroundColor:hexA(P.long,.55),borderColor:P.card,borderWidth:1,pointRadius:4,pointHoverRadius:6},
      {type:"line",label:"Fit",data:[{x:xmin,y:o.a+o.b*xmin},{x:xmax,y:o.a+o.b*xmax}],borderColor:P.ink,borderWidth:2,pointRadius:0}]},
    plugins:[nowLine],
    options:opts({plugins:{legend:{display:false},tooltip:Object.assign(tip({title:i=>ym(i[0].raw.d),label:c=>[` ${predName[pred]}: ${f(c.raw.x,2)}`,` Next 6m change: ${f(c.raw.y,2,true)} pp`]}),{filter:c=>c.datasetIndex===0})},
      scales:{x:{grid:{color:P.grid},border:{display:false},title:{display:true,text:predName[pred]+", z units",color:P.mut,font:{size:12}},ticks:Object.assign(tick(),{callback:v=>f(v,1)})},
              y:{grid:{color:P.grid},border:{display:false},title:{display:true,text:"Change in liquidity YoY, pp",color:P.mut,font:{size:12}},ticks:Object.assign(tick(),{callback:v=>f(v,0,true)})}}})});
}
$("#predSeg").addEventListener("click",e=>{const b=e.target.closest("button");if(!b)return;pred=b.dataset.p;
  document.querySelectorAll("#predSeg button").forEach(x=>x.setAttribute("aria-pressed",String(x===b)));drawScatter();});
function drawBT(){
  let h=`<thead><tr><th>Horizon</th><th>Signal r</th><th>Hit</th><th>Indicators r</th><th>Hit</th><th>−Liquidity z r</th><th>Hit</th></tr></thead><tbody>`;
  [3,6,9,12].forEach(m=>{const a=D.bt[m].all, v3=[a.signal,a.indicators_only,a.minus_liq_z_only], best=Math.max(...v3.map(v=>v.corr));
    h+=`<tr><td>${m} months</td>`+v3.map(v=>`<td class="${v.corr===best?"best":""}">${f(v.corr,2)}</td><td>${Math.round(v.hit*100)}%</td>`).join("")+`</tr>`;});
  const fw=D.bt[6].full_window;
  h+=`<tr class="sep"><td>6m, full 10y windows</td>`+[fw.signal,fw.indicators_only,fw.minus_liq_z_only].map(v=>`<td>${f(v.corr,2)}</td><td>${Math.round(v.hit*100)}%</td>`).join("")+`</tr></tbody>`;
  $("#btTable").innerHTML=h;
  const lab=b=>b.lo<-5?"Below −0.5":b.hi>5?"0.5 and above":`${f(b.lo,1)} to ${f(b.hi,1)}`;
  $("#bandTable").innerHTML=`<thead><tr><th>Signal band</th><th>Months</th><th>Avg change, pp</th><th>Share rising</th></tr></thead><tbody>`+
    D.bands6.map(b=>`<tr><td>${lab(b)}</td><td>${b.n}</td><td class="${b.mean_fwd>=0?"pos":"neg"}">${f(b.mean_fwd,2,true)}</td><td>${isNum(b.up_share)?Math.round(b.up_share*100)+"%":"n/a"}</td></tr>`).join("")+`</tbody>`;
}

/* ---------- lead-lag heat table and small multiples ---------- */
function drawHeat(){
  const L=D.lags;
  let h=`<thead><tr><th>Indicator</th>${L.map(l=>`<th style="text-align:center">${l>0?"+":l<0?MINUS:""}${Math.abs(l)}m</th>`).join("")}</tr></thead><tbody>`;
  D.prof.forEach((row,i)=>{const pk=row.reduce((b,v,j)=>isNum(v)&&Math.abs(v)>Math.abs(row[b]??0)?j:b,0);
    h+=`<tr><td>${N[i]}</td>`+row.map((v,j)=>{const a=isNum(v)?Math.min(.62,Math.abs(v)*.85):0;const bg=isNum(v)?hexA(v>=0?P.pos:P.neg,a):"transparent";
      return`<td class="${j===pk?"peak":""}" style="background:${bg}">${f(v,2)}</td>`}).join("")+`</tr>`;});
  $("#heat").innerHTML=h+"</tbody>";
}
function drawMult(){
  const Rl=D.roll, box=$("#mult");
  if(!box.dataset.built){box.innerHTML=K.map((k,i)=>`<div class="m"><h4>${N[i]}<span id="mv_${k}"></span></h4><div class="chart mini"><canvas id="cm_${k}" role="img" aria-label="Rolling correlation, ${N[i]}"></canvas></div></div>`).join("");box.dataset.built="1"}
  K.forEach((k,i)=>{const v=Rl[k], last=[...v].reverse().find(isNum);
    document.getElementById("mv_"+k).textContent=f(last,2);
    mk("cm_"+k,{type:"line",data:{labels:Rl.d,datasets:[line(N[i],v,P.ink,1.5,{fill:{target:"origin",above:hexA(P.pos,.22),below:hexA(P.neg,.22)}})]},
      options:opts({interaction:{mode:"index",intersect:false},plugins:{legend:{display:false},tooltip:tip({title:t=>ym(Rl.d[t[0].dataIndex]),label:c=>` Avg lead corr: ${f(c.raw,2)}`})},
        scales:{x:timeX(Rl.d,4),y:{min:-1,max:1,grid:{color:c=>c.tick.value===0?P.mut:P.grid},border:{display:false},ticks:Object.assign(tick(),{stepSize:1,callback:v=>f(v,0)})}}})});});
}

/* ---------- liquidity composition ---------- */
function drawLiq(){
  const g=D.liq, ser=[["Central bank assets",P.long],["Broad money",P.pos],["FX reserves ex gold",P.short]];
  mk("cLiq",{type:"line",data:{labels:g.d,datasets:ser.map(([n,c])=>({label:n,data:g.groups[n],backgroundColor:hexA(c,.78),borderColor:P.card,borderWidth:1.5,fill:true,pointRadius:0,pointHoverRadius:3,tension:0}))},
    options:opts({interaction:{mode:"index",intersect:false},plugins:{legend:{display:false},tooltip:tip({title:t=>ym(g.d[t[0].dataIndex]),label:c=>` ${c.dataset.label}: $${f(c.raw,1)}tn`,footer:t=>"Total: $"+f(t.reduce((a,c)=>a+c.raw,0),1)+"tn"})},
      scales:{x:timeX(g.d,4),y:{stacked:true,grid:{color:P.grid},border:{display:false},ticks:Object.assign(tick(),{callback:v=>"$"+v})}}})});
  $("#compTitle").textContent=`Components, ${M.Tg}`;
  let h=`<thead><tr><th>Component</th><th>USD tn</th><th>YoY</th></tr></thead><tbody>`;
  g.comps.forEach((c,i)=>{const first=i===3||i===8;const nm=c.name.replace("FX reserves ","FX reserves, ")+(M.carried[c.name]?` <span class="muted">(held at ${ym(M.carried[c.name])})</span>`:"");
    h+=`<tr class="${first?"sep":""}"><td>${nm}</td><td>${f(c.tn,2)}</td><td class="${c.yoy>=0?"":"neg"}">${f(c.yoy,1,true)}%</td></tr>`;});
  h+=`<tr class="sep"><td><b>Total</b></td><td><b>${f(g.level,1)}</b></td><td>${f(liqNow,1,true)}%</td></tr></tbody>`;
  $("#compTable").innerHTML=h;
}

/* ---------- explorer ---------- */
const sel=$("#expInd"); sel.innerHTML=K.map((k,i)=>`<option value="${k}">${N[i]}</option>`).join(""); sel.value="rate";
let invTouched=false;
function z(arr){const v=arr.filter(isNum),m=v.reduce((a,b)=>a+b,0)/v.length,s=Math.sqrt(v.reduce((a,b)=>a+(b-m)**2,0)/v.length);return arr.map(x=>isNum(x)?(x-m)/s:null)}
const addM=(d,n)=>{let[y,m]=d.split("-").map(Number);m+=n;while(m>12){m-=12;y++}while(m<1){m+=12;y--}return`${y}-${String(m).padStart(2,"0")}`};
function drawExp(){
  const k=sel.value,i=K.indexOf(k),L=+$("#expLead").value;
  if(!invTouched)$("#expInv").checked=(now.cavg[i]??0)<0;
  const inv=$("#expInv").checked;
  const G=D.liq_yoy, I=D.ind[k];
  const start=G.d[0]<I.d[0]?I.d[0]:G.d[0], end=addM(G.d.at(-1),L);
  const months=[];for(let d=start;d<=end;d=addM(d,1))months.push(d);
  const gmap=Object.fromEntries(G.d.map((d,j)=>[d,G.v[j]])), imap=Object.fromEntries(I.d.map((d,j)=>[d,I.v[j]]));
  const gz=z(months.map(d=>gmap[d]??null)), iz=z(months.map(d=>imap[addM(d,-L)]??null)).map(v=>isNum(v)?(inv?-v:v):null);
  const li=D.lags.indexOf(L), c=li>=0?D.prof[i][li]:null;
  $("#expTitle").textContent=`${N[i]} vs liquidity YoY, indicator shifted ${L} months forward`;
  $("#expCap").innerHTML=`Proxy: ${D.proxies[i]}. Correlation at this lead over the latest ten years: <span class="num">${f(c,2)}</span>${inv?" (indicator inverted for display)":""}. Months after ${M.Tg} show where the indicator points liquidity next.`;
  $("#expLeg").textContent=`${N[i]} YoY, z${inv?", inverted":""}, shifted ${L}m`;
  mk("cExp",{type:"line",data:{labels:months,datasets:[line("Liquidity YoY z",gz,P.ink,2),line(N[i],iz,P.pos,1.6)]},
    options:opts({interaction:{mode:"index",intersect:false},plugins:{legend:{display:false},tooltip:tip({title:t=>ym(months[t[0].dataIndex]),label:c=>` ${c.dataset.label}: ${f(c.raw,2)}`})},
      scales:{x:timeX(months,2),y:{grid:{color:P.grid},border:{display:false},ticks:Object.assign(tick(),{callback:v=>f(v,0)})}}})});
}
sel.addEventListener("change",()=>{invTouched=false;drawExp()});$("#expLead").addEventListener("change",drawExp);$("#expInv").addEventListener("change",()=>{invTouched=true;drawExp()});

/* ---------- indicator table ---------- */
function dataTo(k){const cl=M.country_last||{};
  if(cl[k])return Object.entries(cl[k]).map(([c,d])=>`${c} ${d?ym(d).replace(" 20"," ’"):"n/a"}`).join(", ");
  if(k==="usd"&&M.usd_ext)return`BIS to ${ym(M.usd_ext)}, then Fed broad dollar`;return M.T}
function drawTable(){
  const cls=v=>isNum(v)?(v>=0?"pos":"neg"):"";
  let h=`<thead><tr><th>Indicator</th><th>Latest YoY</th><th>Corr 3m</th><th>Corr 6m</th><th>Z t3y</th><th>Z t10y</th><th>Adj. z</th><th>Adj. z, ${ym(p3.T)}</th><th>Adj. z, ${ym(p12.T)}</th><th style="text-align:left">Proxy and latest data</th></tr></thead><tbody>`;
  K.forEach((k,i)=>{const inSum=S7.includes(k);
    h+=`<tr><td>${N[i]}${inSum?"":' <span class="muted">(excl.)</span>'}</td><td>${fu(now.latest[i],i)}</td><td>${f(now.c3[i],2)}</td><td>${f(now.c6[i],2)}</td><td>${f(now.zc[i],2)}</td><td>${f(now.zs[i],2)}</td>
      <td class="${cls(now.adj[i])}">${f(now.adj[i],2)}</td><td>${f(p3.adj[i],2)}</td><td>${f(p12.adj[i],2)}</td><td class="txt">${D.proxies[i]}. ${dataTo(k)}.</td></tr>`;});
  h+=`<tr class="sep"><td>Sum of 7 (ex stocks, crypto)</td><td></td><td></td><td></td><td></td><td></td><td>${f(now.s7,2)}</td><td>${f(p3.s7,2)}</td><td>${f(p12.s7,2)}</td><td class="txt"></td></tr>
     <tr><td>Liquidity YoY z-score, t10y</td><td>${f(liqNow,1,true)}%</td><td></td><td></td><td></td><td></td><td>${f(now.gz,2)}</td><td>${f(p3.gz,2)}</td><td>${f(p12.gz,2)}</td><td class="txt"></td></tr>
     <tr><td><b>Signal</b></td><td></td><td></td><td></td><td></td><td></td><td class="${cls(now.sig)}"><b>${f(now.sig,2)}</b></td><td><b>${f(p3.sig,2)}</b></td><td><b>${f(p12.sig,2)}</b></td><td class="txt">${now.band} now.</td></tr></tbody>`;
  $("#indTable").innerHTML=h;
}

/* ---------- notes ---------- */
function drawNotes(){
  const bad=Object.entries(M.status).filter(([,v])=>String(v).startsWith("FAILED"));
  const carried=Object.entries(M.carried).map(([k,v])=>`${k.replace("FX reserves ","")} (${ym(v)})`).join(", ");
  $("#notes").innerHTML=`
  <div><h3>The calculation</h3><p>For each indicator: YoY change (percentage change for levels, percentage-point change for yields and unemployment; GDP and core CPI enter as YoY rates). Lead correlation = Pearson correlation of indicator YoY at month t with liquidity YoY at t+3 and t+6 over the latest 120 month-pairs. Structural z = latest YoY against its trailing 120 months; cyclical z uses 36. Adjusted z = structural z × the average of the 3m and 6m correlations. Signal = sum of the seven adjusted z-scores excluding stocks and crypto, minus the structural z of liquidity YoY. Stocks and crypto are shown but excluded from the sum because they move with liquidity rather than ahead of it.</p></div>
  <div><h3>Liquidity proxy</h3><p>USD at month-end exchange rates. Central bank assets: Fed (WALCL), ECB (ECBASSETSW), BoJ (JPNASSETS). Broad money: US M2, euro area M3 (ECB), Japan M2 (BoJ API), China M2 (East Money, spliced onto FRED/OECD before 2008), UK M4 (BoE). FX reserves ex gold (IMF via FRED): China, Japan, India, Korea, Saudi Arabia, Brazil. Not covered: PBoC and BoE balance sheets, Swiss and Taiwanese reserves. ${carried?`Reserves held at the last print: ${carried}.`:""} Starts Apr 2003, when the BoJ's M2 series begins.</p></div>
  <div><h3>Indicator proxies</h3><ul>
    <li>Global stock market cap: MSCI ACWI price index in USD.</li>
    <li>Crypto market: Bitcoin (Coinbase), which is most of total crypto market cap; history starts Dec 2014.</li>
    <li>USD NEER: BIS broad nominal effective rate${M.usd_ext?`, extended past ${ym(M.usd_ext)} with the Fed broad dollar index`:""}.</li>
    <li>CVIX: 63-day realised volatility of nine major FX crosses at turnover-style weights. Deutsche Bank's CVIX is implied volatility and not public. Against Cboe EVZ (to Mar 2025) the proxy correlates 0.85 in levels and 0.86 in YoY.</li>
    <li>Global 10y yields: US 50%, Germany 30%, Japan 13%, UK 7% (roughly the Bloomberg Global Aggregate currency mix ex China).</li>
    <li>Global real GDP: quarterly real GDP YoY for China, US, euro area, India, Japan, UK, Korea, Canada at approximate IMF PPP shares, held flat within quarters. China's single-quarter growth is derived from cumulative figures.</li>
    <li>Global core CPI: US, euro area (Eurostat COICOP 2018 series), Japan (ex fresh food and energy), UK, PPP-weighted. China excluded.</li>
    <li>Global unemployment: US, euro area, Japan, UK, Canada, weighted by labour force.</li></ul></div>
  <div><h3>Backtest design and limits</h3><p>From ${M.hist_from}, each month uses indicators to that month and liquidity to the month before, with trailing windows only (minimum 60 months before full 10-year windows exist from ${M.hist_full_from}). Current data vintages are used throughout, so revisions are ignored. Six-month outcomes overlap, so 200-odd monthly observations amount to roughly 30 independent ones; read the correlations as indicative.</p></div>
  <div><h3>Sources</h3><p>FRED (St. Louis Fed), ECB Data Portal, Eurostat, Bank of Japan, Statistics Bureau of Japan (e-Stat), Bank of England, ONS, BIS, MSCI, East Money, Yahoo Finance (MOVE).${bad.length?` Failed on this build: ${bad.map(([k])=>k).join(", ")}.`:" All series loaded on this build."}${nStale?` Live download failed, last good copy used: ${M.stale.map(k=>k.replace(/\.(csv|json)$/,"")).join(", ")}.`:""}${Object.keys(M.fallbacks||{}).length?` Secondary source used: ${Object.entries(M.fallbacks).map(([k,v])=>k+" from "+v).join("; ")}.`:""}</p></div>`;
  $("#foot").textContent=`Built ${M.built}. Regenerate with rebuild.py.`;
}

/* ---------- render ---------- */
function renderAll(){pal();
  Chart.defaults.font.family="IBM Plex Sans"; Chart.defaults.color=P.mut;
  drawCall();drawTiles();drawFindings();drawBreakdown();drawHistory();drawScatter();drawBT();drawHeat();drawMult();drawLiq();drawExp();drawTable();drawNotes();}
renderAll();
let rt; const rer=()=>{clearTimeout(rt);rt=setTimeout(()=>{pal();drawCall();drawBreakdown();drawHistory();drawScatter();drawHeat();drawMult();drawLiq();drawExp();},60)};
matchMedia("(prefers-color-scheme: dark)").addEventListener("change",rer);
new MutationObserver(rer).observe(document.documentElement,{attributes:true,attributeFilter:["data-theme"]});
if(document.fonts&&document.fonts.ready)document.fonts.ready.then(rer);
})();
</script>
"""

OPTIONAL_SERIES = {"evz", "m2_cn_old"}   # validation / back-history only


def main():
    global OFFLINE
    import sys
    OFFLINE = "--offline" in sys.argv
    print("Fetching series ...")
    raw, status = fetch_all(print)
    missing = sorted(k for k, v in status.items() if str(v).startswith("FAILED") and k not in OPTIONAL_SERIES)
    print(f"  {len(status) - len(missing)}/{len(status)} series available ({len(STALE)} from last good download)")
    if missing:
        sys.exit("NOT PUBLISHING: no data, live or cached, for " + ", ".join(missing))
    P = build_panel(raw, print)
    payload = build(raw, status, P)
    payload["meta"]["stale"] = sorted(STALE)
    payload["meta"]["fallbacks"] = dict(FALLBACKS)

    nw = payload["now"]
    if nw["sig"] is None:
        sys.exit("NOT PUBLISHING: the signal could not be computed")
    T = pd.Timestamp(nw["T"] + "-01"); today = pd.Timestamp.today()
    age = (today.year - T.year) * 12 + today.month - T.month
    if age > 3:
        sys.exit(f"NOT PUBLISHING: latest indicator month {payload['meta']['T']} is {age} months old")

    js = json.dumps(payload, separators=(",", ":"))
    html, n = re.subn(r"/\*DATA\*/.*?/\*END\*/", lambda m: "/*DATA*/" + js + "/*END*/", TEMPLATE, flags=re.S)
    assert n == 1, "data placeholder missing from template"
    open(os.path.join(HERE, "index.html"), "w", encoding="utf-8").write(html)
    Hs = payload["hist"]
    with open(os.path.join(HERE, "signal.csv"), "w", encoding="utf-8") as fh:
        fh.write("month,signal,sum_of_7_adjusted,minus_liquidity_z,full_10y_window\n")
        for row in zip(Hs["d"], Hs["sig"], Hs["s7"], Hs["mgz"], Hs["full"]):
            fh.write(",".join("" if v is None else str(v) for v in row) + "\n")
    print(f"Signal {nw['sig']:+.2f} ({nw['band']}) | sum of 7 {nw['s7']:+.2f} | liquidity z {nw['gz']:+.2f} | "
          f"indicators to {payload['meta']['T']}, liquidity to {payload['meta']['Tg']}")
    print(f"Wrote index.html ({len(html)//1024} KB) and signal.csv ({len(Hs['d'])} months)")


if __name__ == "__main__":
    main()
