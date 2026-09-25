#!/usr/bin/env python3
"""Refresh data.json for the Stocks or Bonds calculator.

Daily:   10-year Treasury yield (US Treasury daily par yield curve XML feed)
         S&P 500 close (FRED series SP500, keyless CSV; Yahoo chart API as a fallback)
Weekly:  FactSet Earnings Insight (forward 12-month P/E, the S&P level it is quoted
         against, next calendar year EPS growth). Usually published on Fridays.
Manual:  ERP, beta, dividend yield (edit data.json by hand).
Derived: D1 = spx_ref / fwd_pe, g = rf + beta*erp - D1/spx*100.

Only the standard library plus pypdf is used. Any source that fails or returns
values outside sanity bounds is skipped with a warning and the previous values
are kept. The script exits 0 in that case so the scheduled job stays green.
data.json is rewritten only when a value changed.
"""
import argparse
import csv
import datetime as dt
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

UA = "stocks-or-bonds-updater/1.0 (+https://github.com/condortango/stocks-or-bonds)"
FACTSET_REDIRECT = "https://www.factset.com/earningsinsight"
FACTSET_PATTERN = ("https://advantage.factset.com/hubfs/Website/Resources%20Section/"
                   "Research%20Desk/Earnings%20Insight/EarningsInsight_{mmddyy}.pdf")
TREASURY_XML = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
                "pages/xml?data=daily_treasury_yield_curve&field_tdr_date_value_month={yyyymm}")
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=SP500"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?range=1mo&interval=1d"

BOUNDS = {
    "rf": (0.0, 20.0),
    "spx": (1000.0, 50000.0),
    "fwd_pe": (10.0, 40.0),
    "g1": (-30.0, 60.0),
    "D1": (20.0, 5000.0),
}
MONTHS = ["january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december"]
KEY_ORDER = ["updated_at", "rf", "beta", "erp", "dy", "spx", "fwd_pe", "D1", "g", "g1_default"]


def log(msg):
    print(msg, flush=True)


def warn(msg):
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print("::warning::" + msg, flush=True)
    else:
        print("WARNING: " + msg, file=sys.stderr, flush=True)


def in_bounds(key, v):
    lo, hi = BOUNDS[key]
    return v is not None and lo <= v <= hi


def fetch(url, timeout=40, tries=3):
    """GET url, return (bytes, final_url, content_type). Raises on final failure.
    A 404 is raised immediately without retries."""
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(), r.geturl(), r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            if e.code in (403, 404, 410):
                raise
            last = e
        except Exception as e:  # network errors, timeouts
            last = e
        time.sleep(2 * (i + 1))
    raise last


# ---------------------------------------------------------------- Treasury
def latest_10y(today):
    """Return (value, 'YYYY-MM-DD') for the latest 10-year par yield."""
    ns = {"a": "http://www.w3.org/2005/Atom",
          "m": "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata",
          "d": "http://schemas.microsoft.com/ado/2007/08/dataservices"}
    months = [today.replace(day=1)]
    months.append((months[0] - dt.timedelta(days=1)).replace(day=1))
    for m in months:
        url = TREASURY_XML.format(yyyymm=m.strftime("%Y%m"))
        try:
            body, _, _ = fetch(url)
            root = ET.fromstring(body)
        except Exception as e:
            warn(f"Treasury feed {m:%Y-%m} failed: {e}")
            continue
        rows = []
        for props in root.iter("{%s}properties" % ns["m"]):
            d = props.find("d:NEW_DATE", ns)
            y = props.find("d:BC_10YEAR", ns)
            if d is None or y is None or not (y.text or "").strip():
                continue
            try:
                rows.append((d.text[:10], float(y.text)))
            except ValueError:
                continue
        if rows:
            rows.sort()
            date, val = rows[-1]
            return round(val, 2), date
    return None, None


# ---------------------------------------------------------------- S&P 500
def fred_sp500():
    """Return list of (date, close) from FRED, oldest first."""
    body, _, _ = fetch(FRED_CSV)
    out = []
    for row in csv.reader(io.StringIO(body.decode("utf-8", "replace"))):
        if len(row) < 2 or not re.match(r"\d{4}-\d{2}-\d{2}$", row[0]):
            continue
        try:
            out.append((row[0], float(row[1])))
        except ValueError:
            continue  # FRED marks holidays with "." or blank
    return out


def yahoo_sp500(now_utc):
    """Fallback: completed daily closes from Yahoo's chart API, oldest first."""
    body, _, _ = fetch(YAHOO_CHART)
    j = json.loads(body)["chart"]["result"][0]
    ts = j["timestamp"]
    closes = j["indicators"]["quote"][0]["close"]
    ny = dt.timezone(dt.timedelta(hours=-4))
    try:
        from zoneinfo import ZoneInfo
        ny = ZoneInfo("America/New_York")
    except Exception:
        pass
    now_ny = now_utc.astimezone(ny)
    out = []
    for t, c in zip(ts, closes):
        if c is None:
            continue
        d = dt.datetime.fromtimestamp(t, dt.timezone.utc).astimezone(ny).date()
        if d == now_ny.date() and (now_ny.hour, now_ny.minute) < (16, 30):
            continue  # today's bar is still live
        out.append((d.isoformat(), round(float(c), 2)))
    return out


# ---------------------------------------------------------------- FactSet
def _date_from_url(url):
    m = re.search(r"_(\d{2})(\d{2})(\d{2})[^/_]*\.pdf", url, re.I)
    if not m:
        return None
    try:
        return dt.date(2000 + int(m.group(3)), int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None


def find_factset(today, days_back=14):
    """Return (pdf_bytes, url, report_date) for the newest Earnings Insight PDF found."""
    cands = []
    # 1) factset.com/earningsinsight redirects to the latest PDF.
    try:
        body, url, ctype = fetch(FACTSET_REDIRECT, tries=2)
        if body[:5] == b"%PDF-" and _date_from_url(url):
            cands.append((_date_from_url(url), url, body))
        else:
            warn(f"{FACTSET_REDIRECT} did not resolve to a dated PDF (got {url}, {ctype})")
    except Exception as e:
        warn(f"{FACTSET_REDIRECT} failed: {e}")
    # 2) Walk back over the known file name pattern.
    for k in range(days_back + 1):
        d = today - dt.timedelta(days=k)
        if cands and d <= cands[0][0]:
            break
        url = FACTSET_PATTERN.format(mmddyy=d.strftime("%m%d%y"))
        try:
            body, _, _ = fetch(url, tries=2)
        except Exception:
            continue
        if body[:5] == b"%PDF-":
            cands.append((d, url, body))
            break
    if not cands:
        return None, None, None
    cands.sort(key=lambda c: c[0], reverse=True)
    d, url, body = cands[0]
    return body, url, d


def pdf_text(pdf_bytes):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf_bytes))
    txt = "\n".join((p.extract_text() or "") for p in reader.pages)
    return re.sub(r"\s+", " ", txt)


def _num(s):
    return float(s.replace(",", ""))


def parse_factset(text, report_date):
    """Extract forward P/E, stated S&P close (+ date if stated), and next-CY EPS growth."""
    out = {}
    # Forward 12-month P/E: majority vote across all "... P/E ratio (for the S&P 500) is X" hits.
    pes = [float(x) for x in re.findall(
        r"forward 12\s*-\s*month P/E ratio (?:for the S&P 500 )?(?:is|was) (\d{1,2}\.\d)\b", text, re.I)]
    if pes:
        best = max(pes, key=lambda v: (pes.count(v), -pes.index(v)))
        if len(set(pes)) > 1:
            warn(f"FactSet P/E mentions disagree {pes}; using {best}")
        out["fwd_pe"] = best
    # S&P level the report is quoted against ("... above the closing price of 7,704.13").
    m = re.search(r"(?:above|below) the closing price of \$?(\d{1,2},?\d{3}\.\d{2})", text, re.I)
    if m:
        out["spx_ref"] = _num(m.group(1))
    m = re.search(r"closing price \(?(?:on|as of) (" + "|".join(MONTHS) + r") (\d{1,2})\b", text, re.I)
    if m:
        mon = MONTHS.index(m.group(1).lower()) + 1
        yr = report_date.year - (1 if mon > report_date.month else 0)
        try:
            out["spx_ref_date"] = dt.date(yr, mon, int(m.group(2))).isoformat()
        except ValueError:
            pass
    # Calendar year EPS growth: "For CY 2027, analysts are projecting earnings growth of 15.4%"
    # The "Forward Estimates" line ("... of 15.4% and revenue growth of 9.2%") wins over the
    # looser summary phrasing, which FactSet sometimes rounds differently.
    def grab(pattern):
        found = {}
        for yr, kind, val in re.findall(pattern, text, re.I):
            v = float(val)
            found.setdefault(int(yr), -abs(v) if kind.lower() == "decline" else v)
        return found
    head = r"For CY ?(\d{4}),? analysts are "
    num = r"(growth|decline) (?:rate )?of (-?\d{1,2}\.\d)\s*%"
    growth = grab(head + r"(?:projecting|predicting|calling for) (?:an? )?(?:\(year-over-year\) )?earnings " + num)
    growth.update(grab(head + r"projecting (?:an? )?earnings " + num + r" and (?:a )?revenue"))
    want = report_date.year + 1
    if want in growth:
        out["g1_year"], out["g1"] = want, growth[want]
    elif growth:
        yr = max(growth)
        if yr >= report_date.year:
            warn(f"FactSet report has no CY{want} growth; using CY{yr}")
            out["g1_year"], out["g1"] = yr, growth[yr]
    return out


def prev_weekday(d):
    d -= dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


# ---------------------------------------------------------------- main
def ordered(data):
    return {k: data[k] for k in KEY_ORDER if k in data} | {k: v for k, v in data.items() if k not in KEY_ORDER}


def recompute(d):
    fp = d["fwd_pe"]
    D1 = round(fp["spx_ref"] / fp["value"], 2)
    d["D1"]["value"] = D1
    d["D1"]["as_of"] = fp["spx_ref_date"]
    r = d["rf"]["value"] + d["beta"]["value"] * d["erp"]["value"]
    d["g"]["value"] = round(r - D1 / d["spx"]["value"] * 100, 3)
    d["g"]["as_of"] = max(d["spx"]["as_of"], d["rf"]["as_of"])


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data.json"))
    ap.add_argument("--dry-run", action="store_true", help="print the result, do not write")
    ap.add_argument("--today", help="override today's date (YYYY-MM-DD) for testing")
    ap.add_argument("--factset-pdf", help="parse this local PDF instead of downloading (testing)")
    ap.add_argument("--factset-date", help="report date for --factset-pdf (YYYY-MM-DD)")
    args = ap.parse_args()

    path = os.path.abspath(args.data)
    with open(path, encoding="utf-8") as f:
        old = json.load(f)
    d = json.loads(json.dumps(old))
    now = dt.datetime.now(dt.timezone.utc)
    today = dt.date.fromisoformat(args.today) if args.today else now.date()

    # 10-year yield
    try:
        rf, rf_date = latest_10y(today)
        if rf is None:
            warn("No 10-year yield found; keeping previous")
        elif not in_bounds("rf", rf):
            warn(f"10-year yield {rf} out of bounds; keeping previous")
        elif rf_date < d["rf"]["as_of"]:
            warn(f"Treasury latest {rf_date} is older than data.json {d['rf']['as_of']}; keeping previous")
        else:
            log(f"10-year yield: {rf}% on {rf_date}")
            d["rf"]["value"], d["rf"]["as_of"] = rf, rf_date
    except Exception as e:
        warn(f"Treasury step failed: {e}")

    # S&P 500 close
    series, src = [], None
    try:
        series, src = fred_sp500(), "FRED SP500"
    except Exception as e:
        warn(f"FRED SP500 failed: {e}")
    if not series:
        try:
            series, src = yahoo_sp500(now), "Yahoo ^GSPC"
        except Exception as e:
            warn(f"Yahoo fallback failed: {e}")
    if series:
        s_date, s_val = series[-1]
        if not in_bounds("spx", s_val):
            warn(f"S&P 500 {s_val} out of bounds; keeping previous")
        elif s_date < d["spx"]["as_of"]:
            log(f"S&P 500 latest close {s_val} on {s_date} ({src}) is older than data.json "
                f"{d['spx']['as_of']}; keeping previous")
        else:
            log(f"S&P 500 close: {s_val} on {s_date} ({src})")
            d["spx"].update(value=s_val, as_of=s_date, time=None)
            d["spx"]["source"] = f"S&P 500 close ({src})"
            d["spx"]["url"] = FRED_CSV if src.startswith("FRED") else YAHOO_CHART

    # FactSet
    try:
        if args.factset_pdf:
            with open(args.factset_pdf, "rb") as f:
                pdf = f.read()
            url = os.path.basename(args.factset_pdf)
            rdate = dt.date.fromisoformat(args.factset_date) if args.factset_date else _date_from_url(url)
        else:
            pdf, url, rdate = find_factset(today)
        if not pdf:
            warn("No FactSet Earnings Insight PDF found; keeping previous")
        elif rdate.isoformat() <= d["fwd_pe"]["report_date"]:
            log(f"FactSet latest report {rdate} ({url}) is not newer than data.json; nothing to do")
        else:
            fs = parse_factset(pdf_text(pdf), rdate)
            log(f"FactSet {rdate}: {fs}  [{url}]")
            ok = in_bounds("fwd_pe", fs.get("fwd_pe")) and in_bounds("g1", fs.get("g1"))
            if not ok:
                warn(f"FactSet parse incomplete or out of bounds ({fs}); keeping previous")
            else:
                ref, ref_date = fs.get("spx_ref"), fs.get("spx_ref_date")
                if ref is not None and not ref_date:
                    hit = [dd for dd, v in series if abs(v - ref) < 0.005]
                    ref_date = hit[-1] if hit else prev_weekday(rdate).isoformat()
                if ref is None:
                    target = prev_weekday(rdate).isoformat()
                    prior = [(dd, v) for dd, v in series if dd <= target]
                    if prior:
                        ref_date, ref = prior[-1]
                        log(f"No stated S&P level in report; using close {ref} on {ref_date}")
                if ref is None or not in_bounds("spx", ref) or not in_bounds("D1", ref / fs["fwd_pe"]):
                    warn("Could not establish the S&P level for the FactSet P/E; keeping previous")
                else:
                    d["fwd_pe"].update(value=fs["fwd_pe"], spx_ref=ref, spx_ref_date=ref_date,
                                       report_date=rdate.isoformat(), url=url)
                    d["g1_default"].update(value=fs["g1"], year=fs["g1_year"], as_of=rdate.isoformat(), url=url,
                                           source=f"FactSet Earnings Insight, CY{fs['g1_year']} S&P 500 EPS growth estimate")
    except Exception as e:
        warn(f"FactSet step failed: {e}")

    recompute(d)
    if not (in_bounds("D1", d["D1"]["value"]) and 0 < d["g"]["value"] < d["rf"]["value"] + d["beta"]["value"] * d["erp"]["value"]):
        warn(f"Derived values look wrong (D1={d['D1']['value']}, g={d['g']['value']}); not writing")
        return 0

    strip = lambda x: {k: v for k, v in x.items() if k != "updated_at"}
    if strip(d) == strip(old):
        log("No changes; data.json left as is")
        return 0
    d["updated_at"] = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    text = json.dumps(ordered(d), indent=2, ensure_ascii=True) + "\n"
    if args.dry_run:
        log("--dry-run, would write:\n" + text)
        return 0
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)
    log(f"Wrote {path}: D1={d['D1']['value']} g={d['g']['value']} rf={d['rf']['value']} spx={d['spx']['value']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
