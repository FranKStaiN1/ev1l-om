#!/usr/bin/env python3
"""Ev1L 3ye — OpenMarket GEX runner for GitHub Actions (no server, no card).

Every run: launches headless Chromium with YOUR OpenMarket session (cookies
stored as a GitHub Secret), opens your GC chart, reads Call Wall / Put Wall /
Gamma Flip / Max Pain off the page and POSTs them to your Ev1L 3ye app.

Environment (all from GitHub Secrets):
  OM_STATE  your OpenMarket session: either a Playwright storage-state JSON
            ({"cookies":[...],"origins":[...]}) or an EditThisCookie export
            (a plain [...] cookie list). NEVER commit this to the repo.
  OM_URL    your OpenMarket GC chart URL.
  OM_KEY    your Ev1L webhook key (the one from the app's Settings panel).
  OM_ENDPOINT (optional) defaults to the Ev1L levels receiver.

Exit codes: 0 ok · 2 session expired (re-export cookies) · 3 levels unreadable.
"""
import json, os, sys, tempfile, time, urllib.request
from datetime import datetime, timezone

ENDPOINT = os.environ.get("OM_ENDPOINT", "https://ev1l-3ye.vercel.app/api/openmarket/levels?asset=gold")

def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)

EXTRACT_JS = r"""
() => {
  const numFrom = (str) => {
    if (!str) return null;
    const ms = String(str).match(/\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d{3,}(?:\.\d+)?/g) || [];
    for (const m of ms) { const v = parseFloat(m.replace(/,/g, '')); if (v >= 500 && v <= 20000) return v; }
    return null;
  };
  const PATTERNS = [
    {k: 'call', re: /call\s*wall/i},
    {k: 'put',  re: /put\s*wall/i},
    {k: 'flip', re: /(gamma\s*flip|zero\s*gamma|gex\s*flip|gamma\s*zero)/i},
    {k: 'pain', re: /max\s*pain/i},
  ];
  const out = {};
  const text = document.body ? document.body.innerText : '';
  const lines = String(text || '').split(/\n+/).map(x => x.trim()).filter(Boolean);
  lines.forEach((line, i) => {
    PATTERNS.forEach(p => {
      if (out[p.k] != null || !p.re.test(line)) return;
      let v = numFrom(line);
      if (v == null && lines[i + 1]) v = numFrom(lines[i + 1]);
      if (v == null && lines[i + 2] && !/wall|flip|pain/i.test(lines[i + 2])) v = numFrom(lines[i + 2]);
      if (v != null) out[p.k] = v;
    });
  });
  if (out.flip == null) {
    for (let i = 0; i < lines.length; i++) {
      if (/^\s*(gex\s*)?flip\b/i.test(lines[i])) { const v = numFrom(lines[i]) || numFrom(lines[i + 1] || ''); if (v != null) { out.flip = v; break; } }
    }
  }
  if (out.call == null || out.put == null || out.flip == null) {
    const els = document.querySelectorAll('div,span,td,li,dt,dd');
    for (let i = 0; i < els.length; i++) {
      const el = els[i], t = (el.textContent || '').trim();
      if (!t || t.length > 60) continue;
      for (const p of PATTERNS) {
        if (out[p.k] != null || !p.re.test(t)) continue;
        let node = el, v = null, hops = 0;
        while (node && hops < 4 && v == null) { v = numFrom(node.textContent); node = node.parentElement; hops++; }
        if (v != null) out[p.k] = v;
      }
    }
  }
  const loggedOut = /sign in|log in|create account/i.test(text.slice(0, 4000)) && out.call == null;
  return {...out, _loggedOut: loggedOut, _title: document.title || ''};
}
"""

def plausible(f):
    if not f or f.get("call") is None or f.get("put") is None or f.get("flip") is None:
        return False
    if not f["put"] < f["call"]:
        return False
    mid = (f["call"] + f["put"]) / 2
    return all(mid * 0.7 < f[k] < mid * 1.4 for k in ("call", "put", "flip"))

def _load_state_json(raw):
    """Tolerant OM_STATE parsing: pure JSON first, then the outermost
    {...} or [...] span (in case something extra got pasted around it).
    Returns parsed data or raises ValueError with a clear hint."""
    raw = (raw or "").strip().lstrip("\ufeff")
    try:
        return json.loads(raw)
    except Exception:
        pass
    for a, b in (("[", "]"), ("{", "}")):
        i, j = raw.find(a), raw.rfind(b)
        if 0 <= i < j:
            try:
                return json.loads(raw[i:j + 1])
            except Exception:
                continue
    raise ValueError("OM_STATE is not valid JSON - re-export the cookies "
                     "with EditThisCookie and paste the whole [...] text again")


def build_state_file(raw):
    """Accept storage-state JSON or an EditThisCookie [...] export."""
    data = _load_state_json(raw)
    if isinstance(data, dict) and "cookies" in data:
        state = data
    elif isinstance(data, list):
        cookies = []
        for c in data:
            if not c.get("name") or "value" not in c or not c.get("domain"):
                continue
            nc = {"name": c["name"], "value": c["value"], "domain": c["domain"],
                  "path": c.get("path") or "/",
                  "expires": float(c.get("expirationDate") or c.get("expires") or -1),
                  "httpOnly": bool(c.get("httpOnly")), "secure": bool(c.get("secure"))}
            ss = str(c.get("sameSite") or "").lower()
            if ss in ("strict", "lax", "none"):
                nc["sameSite"] = {"strict": "Strict", "lax": "Lax", "none": "None"}[ss]
            cookies.append(nc)
        state = {"cookies": cookies, "origins": []}
    else:
        raise ValueError("OM_STATE format not recognised")
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(state, f)
    return path

def main():
    raw = os.environ.get("OM_STATE", "").strip()
    url = os.environ.get("OM_URL", "").strip()
    key = os.environ.get("OM_KEY", "").strip()
    if not raw or not url or not key:
        log("ERROR: OM_STATE / OM_URL / OM_KEY secrets are missing"); return 3
    if "ev1l-3ye" in url or "/api/openmarket" in url:
        log("ERROR: OM_URL must be your OpenMarket GC CHART page (the page in your "
            "browser where you SEE the Call Wall / Put Wall / Gamma Flip numbers) - "
            "not the Ev1L app address. Copy it from your browser address bar.")
        return 3
    try:
        state_path = build_state_file(raw)
    except Exception as e:
        log(f"ERROR: {e}"); return 3
    from playwright.sync_api import sync_playwright
    found, logged_out = None, False
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(storage_state=state_path, viewport={"width": 1440, "height": 900})
        pg = ctx.new_page()
        pg.goto(url, wait_until="domcontentloaded", timeout=120000)
        try:
            log(f"opened: title={pg.title()!r} url={pg.url}")
        except Exception:
            pass
        last_got = None
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                got = pg.evaluate(EXTRACT_JS)
            except Exception:
                got = None
            if got:
                last_got = got
            if got and plausible(got):
                found = got; break
            if got and got.get("_loggedOut"):
                logged_out = True; break
            time.sleep(5)
        browser.close()
    if logged_out:
        print("::error::COOKIES EXPIRED — re-export your OpenMarket cookies into the OM_STATE secret (see README).")
        return 2
    if not found:
        seen = {k: (last_got or {}).get(k) for k in ("call", "put", "flip", "pain")} if last_got else None
        log(f"ERROR: levels not readable. Numbers found on the page: {seen} — "
            "open the same chart in your own browser: the GEX indicator with "
            "Call Wall / Put Wall / Gamma Flip must be VISIBLE on it, and the "
            "OM_URL secret must be that page's address.")
        return 3
    payload = {"call_wall": found["call"], "put_wall": found["put"], "flip": found["flip"],
               "max_pain": found.get("pain"), "source": "openmarket-actions"}
    req = urllib.request.Request(ENDPOINT, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "X-OM-Key": key}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        log(f"sent {payload} -> HTTP {r.status} {r.read()[:200]!r}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
