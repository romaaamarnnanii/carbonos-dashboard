#!/usr/bin/env python3
"""
Scrapes the Climes GTM "War Room" dashboard and writes a compact per-industry
summary to gtm.json, which the CarbonOS dashboard's "Where we have pull" panel
blends into the Pull score (call signal + GTM pipeline/inbound signal).

Runs daily via .github/workflows/gtm-sync.yml. Because the GTM page is a
client-rendered SPA with no data API, this renders it headlessly (Playwright)
and parses the visible Kanban. Layout-sensitive by nature — if the GTM page
changes, update the parser below.

Usage:
  python scrape_gtm.py                 # render live + write gtm.json
  python scrape_gtm.py --file t.txt    # parse a saved innerText dump (for tests)
"""
import json, re, sys, datetime

GTM_URL = "https://gtm-september.climes.io/"
FRAMEWORKS = ["CBAM", "PCAF", "SBTI", "EPD", "EUDR", "PCF", "CCTS", "GHG"]
STAGES = ["QUEUED", "SENT", "REPLIED", "CALL", "PROPOSAL", "WON"]
STAGE_W = {"QUEUED": 0.5, "SENT": 1, "REPLIED": 2, "CALL": 3, "PROPOSAL": 5, "WON": 8}

# account name -> industry (extend as the motion adds accounts)
NAME_IND = {
    "JSW Steel": "Metals", "Hindalco Industries": "Metals", "Vedanta Aluminium": "Metals",
    "Jindal Stainless": "Metals", "Ratnamani Metals & Tubes": "Metals", "Venus Pipes & Tubes": "Metals",
    "Amarinox": "Metals", "Viraj Profiles": "Metals", "Centravis": "Metals", "Aperam": "Metals",
    "Gulf Extrusions": "Metals", "Pradeep Metals": "Metals",
    "Sundram Fasteners": "Automotive", "SKF India": "Automotive", "Bharat Forge": "Automotive",
    "TCS": "IT Services", "Infosys": "IT Services", "Tech Mahindra": "IT Services",
    "Nestle India": "FMCG", "Asian Paints": "FMCG",
    "Aarti Industries": "Chemicals", "Anupam Rasayan": "Chemicals",
    "Trident": "Paper & Packaging", "JK Paper": "Paper & Packaging", "Andhra Paper": "Paper & Packaging",
    "Pakka": "Paper & Packaging", "Satia Industries": "Paper & Packaging",
    "Polycab India": "Manufacturing", "Suzlon": "Manufacturing",
    "Nororbis Itus": "Manufacturing", "Novorbis Itus": "Manufacturing",
    "Apollo Hospitals": "Healthcare", "Zydus Lifesciences": "Healthcare",
    "Bajaj Finserv": "Banking & Finance", "Tata AIG": "Banking & Finance", "Saudi Awwal Bank": "Banking & Finance",
    "LATAM Airlines": "Aviation",
}
FRAMEWORK_IND = {"PCAF": "Banking & Finance", "CCTS": "Paper & Packaging", "CBAM": "Metals",
                 "SBTI": "IT Services", "EPD": "Manufacturing", "EUDR": "FMCG", "PCF": "Manufacturing", "GHG": "Other"}

def industry_for(name, framework):
    if name in NAME_IND:
        return NAME_IND[name]
    return FRAMEWORK_IND.get(framework, "Other")

OWNERS = {"Ani", "Aravind", "Manasvi"}

def parse_text(txt):
    lines = [l.strip() for l in txt.split("\n")]
    n = len(lines)
    # start at the Kanban (first bare stage header immediately followed by a count)
    start = 0
    for i, l in enumerate(lines):
        if l in STAGES and i + 1 < n and re.fullmatch(r"\d+", lines[i + 1] or ""):
            start = i
            break
    stage = None
    accounts = []
    i = start
    while i < n:
        l = lines[i]
        if l in STAGES:                       # stage column header + its count
            stage = l
            i += 2
            continue
        if not l:
            i += 1
            continue
        # `l` is a candidate account name — a real card has a framework within a few lines
        fw, j = None, i + 1
        while j < n and j < i + 7:
            s = lines[j]
            if s in STAGES:
                break
            if s in FRAMEWORKS:
                fw = s
                break
            j += 1
        if not fw:
            i += 1                            # divider / count / stray line
            continue
        if stage:
            accounts.append({"name": l, "stage": stage, "framework": fw,
                             "industry": industry_for(l, fw)})
        # advance past this card: framework, the 1/2/3 channel marks, owner, age
        i = j + 1
        while i < n and lines[i] in ("1", "2", "3"):
            i += 1
        if i < n and lines[i] in OWNERS:
            i += 1
        if i < n and (re.fullmatch(r"\d+d", lines[i]) or lines[i] in ("—", "")):
            i += 1
    uniq = accounts

    def num_after(label):
        for i, l in enumerate(lines):
            if l.strip().lower() == label.lower() and i + 1 < len(lines):
                m = re.search(r"\d[\d,]*", lines[i + 1])
                if m: return int(m.group().replace(",", ""))
        return None

    funnel = {"contacted": num_after("Contacted"), "replied": num_after("Replied"),
              "call": num_after("Call"), "proposal": num_after("Proposal"), "won": num_after("Won")}

    by = {}
    for a in uniq:
        o = by.setdefault(a["industry"], {"accounts": 0, "gtmPull": 0.0, "replies": 0,
                                          "stages": {s: 0 for s in STAGES}})
        o["accounts"] += 1
        o["gtmPull"] += STAGE_W.get(a["stage"], 0)
        o["stages"][a["stage"]] += 1
        if a["stage"] in ("REPLIED", "CALL", "PROPOSAL", "WON"):
            o["replies"] += 1
    for o in by.values():
        o["gtmPull"] = round(o["gtmPull"], 2)

    snap = ""
    m = re.search(r"\n(\d{1,2} \w+ \d{4},[^\n]*IST)", txt)
    if m: snap = m.group(1).strip()

    return {
        "generatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "snapshotLabel": snap,
        "source": GTM_URL,
        "funnel": funnel,
        "accountsParsed": len(uniq),
        "byIndustry": by,
        "accounts": uniq,
    }

def render_live():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.goto(GTM_URL, wait_until="networkidle", timeout=60000)
        pg.wait_for_timeout(4000)
        txt = pg.inner_text("main")
        b.close()
        return txt

def main():
    if "--file" in sys.argv:
        txt = open(sys.argv[sys.argv.index("--file") + 1]).read()
    else:
        txt = render_live()
    data = parse_text(txt)
    out = "gtm.json"
    json.dump(data, open(out, "w"), indent=1)
    print(f"wrote {out}: {data['accountsParsed']} accounts across {len(data['byIndustry'])} industries")
    for ind, o in sorted(data["byIndustry"].items(), key=lambda kv: -kv[1]["gtmPull"]):
        print(f"  {ind:22} pull={o['gtmPull']:5}  accounts={o['accounts']}  replies={o['replies']}")

if __name__ == "__main__":
    main()
