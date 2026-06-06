#!/usr/bin/env python3
"""
FootyStream auto-scraper → matches.json
- Scrapes footystream.pk homepage
- Resolves each event's first stream → direct .m3u8 URL
- Writes matches.json (committed by GitHub Action)

Output format (matches.json):
{
  "generatedAt": "ISO-8601",
  "count": N,
  "matches": [
    {
      "id": "abc123",
      "league": "...", "leagueLogo": "...",
      "team1": "...", "team2": "...",
      "team1Logo": "...", "team2Logo": "...",
      "status": "Live"/"3h 12m"/...,
      "startTime": "unix-ms", "endTime": "unix-ms",
      "eventUrl": "...",
      "streamUrl": "https://.../index.m3u8",
      "referer":   "https://...",
      "origin":    "https://..."
    }, ...
  ]
}
"""
import json, re, sys, time, hashlib, urllib.parse, datetime, concurrent.futures as cf
import urllib.request, ssl

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
CTX = ssl.create_default_context(); CTX.check_hostname=False; CTX.verify_mode=ssl.CERT_NONE
HOME = "https://footystream.pk/"

def fetch(url, referer=""):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9",
        **({"Referer": referer} if referer else {})
    })
    try:
        with urllib.request.urlopen(req, timeout=20, context=CTX) as r:
            return r.read().decode("utf-8", "ignore")
    except Exception as e:
        print(f"fetch fail {url}: {e}", file=sys.stderr); return ""

def decode(s):
    import html
    return re.sub(r"<[^>]+>", "", html.unescape(s or "")).strip()

# ─── HOMEPAGE ───
def scrape_homepage():
    html = fetch(HOME)
    if not html: return []
    matches = []
    leagues = [(m.start(), decode(m.group(3)), m.group(2)) for m in re.finditer(
        r'<div class="flex items-center gap-3 mb-3">\s*<img alt="([^"]*) Logo"[^>]*src="([^"]*)"[^>]*>\s*<div class="text-white font-semibold text-sm">([^<]+)</div>',
        html, re.S)]
    for mb in re.finditer(r'<a\s+href="(https://footystream\.pk/events?/[^"]+)">([\s\S]*?)</a>', html, re.S):
        pos = mb.start(); event_url = mb.group(1); block = mb.group(2)
        league=""; league_logo=""
        for lp, lname, llogo in leagues:
            if lp < pos: league, league_logo = lname, llogo
        start=end=status=""
        cm = re.search(r'data-start="([^"]*)"[^>]*data-end="([^"]*)"[^>]*>([^<]*)', block)
        if cm: start, end, status = cm.group(1), cm.group(2), decode(cm.group(3))
        team1=team2=t1l=t2l=""
        teams = re.findall(
            r'<div class="flex gap-2 items-center">\s*<img[^>]*src="([^"]*)"[^>]*alt="([^"]*)"[^>]*>\s*([\s\S]*?)</div>',
            block, re.S)
        if len(teams) >= 2:
            t1l, _, team1 = teams[0]; t2l, _, team2 = teams[1]
            team1, team2 = decode(team1), decode(team2)
        elif teams:
            t1l, _, team1 = teams[0]; team1 = decode(team1)
        if not team1 and not team2: continue
        mid = hashlib.md5(event_url.encode()).hexdigest()[:10]
        matches.append({
            "id": mid, "league": league, "leagueLogo": league_logo,
            "team1": team1, "team2": team2, "team1Logo": t1l, "team2Logo": t2l,
            "status": status, "startTime": start, "endTime": end,
            "eventUrl": event_url,
        })
    return matches

# ─── EVENT PAGE ───
def scrape_event(url):
    html = fetch(url, HOME)
    if not html: return []
    out = []
    for row in re.finditer(r'<tr class="hover:bg-neutral-600[^"]*">([\s\S]*?)</tr>', html, re.S):
        rh = row.group(1)
        tds = [decode(t) for t in re.findall(r'<td[^>]*>([\s\S]*?)</td>', rh, re.S)]
        wm = re.search(r'href="([^"]*footystream[^"]*)"', rh)
        if not wm: continue
        out.append({"label": tds[1] if len(tds)>1 else "", "watchUrl": wm.group(1)})
    return out

# ─── STREAM RESOLVER ───
def extract_vars(html):
    vars = {}
    for m in re.finditer(r'(?:var\s+)?([A-Za-z_$][\w$]*)\s*=\s*["\']([^"\']+)["\']\s*;', html):
        vars[m.group(1)] = m.group(2)
    for m in re.finditer(r'(?:var\s+)?([A-Za-z_$][\w$]*)\s*=\s*((?:[\'"][^\'"]*[\'"]\s*\+\s*)*[\'"][^\'"]*[\'"])\s*;', html):
        joined = "".join(re.findall(r'[\'"]([^\'"]*)[\'"]', m.group(2)))
        if len(joined) > len(vars.get(m.group(1), "")): vars[m.group(1)] = joined
    return vars

def extract_m3u8(html, page_url, parent_vars=None):
    vars = dict(parent_vars or {}); vars.update(extract_vars(html))
    pu = urllib.parse.urlparse(page_url); origin = f"{pu.scheme}://{pu.netloc}"

    # Direct m3u8/mpd
    m = re.search(r'https?://[^"\'\s+]+\.(?:m3u8|mpd)[^"\'\s]*', html, re.I)
    if m: return {"streamUrl": m.group(0), "referer": page_url, "origin": origin}

    # /hls/ pattern: base + "/hls/" + channel + ".m3u8"
    ub = re.search(r'var\s+\w+\s*=\s*(\w+)\s*\+\s*["\']\/hls\/["\']\s*\+\s*(\w+)\s*\+\s*["\']\.m3u8["\']', html, re.I)
    if ub:
        base = vars.get(ub.group(1), ""); ch = vars.get(ub.group(2), "")
        if base and ch: return {"streamUrl": f"{base}/hls/{ch}.m3u8", "referer": page_url, "origin": origin}

    # char-array obfuscation
    best = None
    for am in re.finditer(r'\[\s*((?:"[^"]{1,4}"\s*,\s*){10,}"[^"]{1,4}")\s*\]', html):
        try:
            items = json.loads("[" + am.group(1) + "]")
            joined = "".join(items)
            if ".m3u8" in joined and joined.startswith("http"):
                if not best or len(joined) > len(best): best = joined
        except: pass
    if best: return {"streamUrl": best, "referer": page_url, "origin": origin}

    # source/src/file config
    m = re.search(r'(?:source|src|file)\s*:\s*["\']([^"\']+\.(?:m3u8|mpd|mp4)[^"\']*)', html, re.I)
    if m: return {"streamUrl": m.group(1).replace("\\/", "/"), "referer": page_url, "origin": origin}
    return None

def resolve_stream(watch_url):
    try:
        html = fetch(watch_url, HOME)
        if not html: return None
        r = extract_m3u8(html, watch_url)
        if r: return r
        im = re.search(r'<iframe[^>]*src=["\']([^"\']+)["\']', html, re.I)
        if not im: return None
        iframe = im.group(1)
        if not iframe.startswith("http"):
            iframe = ("https:" + iframe) if iframe.startswith("//") else urllib.parse.urljoin(watch_url, iframe)
        embed = fetch(iframe, watch_url)
        if not embed: return None
        vars = extract_vars(embed)
        sm = re.search(r'<script[^>]+src=["\']([^"\']+(?:ano\d*|footy)\.js[^"\']*)["\']', embed)
        if sm:
            su = sm.group(1)
            if not su.startswith("http"):
                su = ("https:" + su) if su.startswith("//") else urllib.parse.urljoin(iframe, su)
            sj = fetch(su, iframe)
            if sj:
                bm = re.search(r'(https?://[^\s"\']+?(?:atofplay|embed|player)\.php\?v=)', sj)
                if bm:
                    fid = vars.get("fid",""); secure = vars.get("v_con",""); exp = vars.get("v_dt","")
                    if fid:
                        purl = f"{bm.group(1)}{fid}&secure={secure}&expires={exp}"
                        ph = fetch(purl, iframe)
                        if ph:
                            r = extract_m3u8(ph, purl, vars)
                            if r: return r
        return extract_m3u8(embed, iframe, vars)
    except Exception as e:
        print(f"resolve fail {watch_url}: {e}", file=sys.stderr)
        return None

def enrich(m):
    streams = scrape_event(m["eventUrl"])
    for s in streams:
        r = resolve_stream(s["watchUrl"])
        if r and r.get("streamUrl"):
            m["streamUrl"] = r["streamUrl"]
            m["referer"]   = r.get("referer","")
            m["origin"]    = r.get("origin","")
            return m
    m["streamUrl"] = None; m["referer"] = None; m["origin"] = None
    return m

def main():
    print("Scraping homepage…", file=sys.stderr)
    matches = scrape_homepage()
    print(f"Found {len(matches)} matches. Resolving streams…", file=sys.stderr)
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        matches = list(ex.map(enrich, matches))
    out = {
        "generatedAt": datetime.datetime.utcnow().isoformat() + "Z",
        "count": len(matches),
        "matches": matches,
    }
    with open("matches.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    resolved = sum(1 for m in matches if m.get("streamUrl"))
    print(f"Wrote matches.json — {resolved}/{len(matches)} resolved.", file=sys.stderr)

if __name__ == "__main__":
    main()
