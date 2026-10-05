#!/usr/bin/env python3
"""
Scrapes the Climes GTM "War Room" dashboard and writes a compact per-industry
summary to gtm.json, which the CarbonOS dashboard's "Where we have pull" panel
blends into the Pull score (call signal + GTM engagement signal).

Runs daily via .github/workflows/gtm-sync.yml. The GTM page is a client-rendered
SPA, so this renders it headlessly (Playwright), opens the "Motion accounts" tab
(the full account table, not just the cards visible on the Kanban) and parses its
text. Layout-sensitive by nature: if the GTM page changes, update parse_text().

PRIVACY: this repo and gtm.json are PUBLIC. Only account name, industry, framework,
country, stage and *derived* signals (inbound / hot / cold / rating) are written.
Contact names, emails and next-action notes are read to derive those signals and
then dropped. Never add them to the output.

Pull model (what "pull" means here):
  * Outbound targets we chose (QUEUED / SENT) are reach, not pull, so they weigh 0.
  * Engagement weighs by depth: REPLIED 1, CALL upcoming 2, CALL held 3,
    PROPOSAL 5, WON 8.
  * x1.5 if the account came to us inbound.
  * x temperature from the GTM notes: a standup "n/10" rating if present
    (n/5, clamped 0.5-1.8), else hot 1.5 / cold 0.5 / neutral 1.
  * The page rate-adjusts per industry (pull / sqrt(accounts reached)), so an
    industry is not "strongest" just because we emailed it the most.

Usage:
  python scrape_gtm.py                 # render live + write gtm.json
  python scrape_gtm.py --file t.txt    # parse a saved innerText dump (for tests)
"""
import json, math, re, sys, datetime

GTM_URL = "https://gtm-september.climes.io/"
ENGAGED = ("REPLIED", "CALL", "PROPOSAL", "WON")
STAGE_W = {"QUEUED": 0, "SENT": 0, "REPLIED": 1, "CALL": 3, "PROPOSAL": 5, "WON": 8, "LOST": 0}
CALL_UPCOMING_W = 2

# account name -> industry. Checked first; extend as the motion adds accounts.
NAME_IND = {
    # metals (steel, aluminium, stainless, forgings, wire, tubes)
    "JSW Steel": "Metals", "Hindalco Industries": "Metals", "Vedanta Aluminium": "Metals",
    "Jindal Stainless": "Metals", "Ratnamani Metals & Tubes": "Metals", "Venus Pipes & Tubes": "Metals",
    "Amarinox": "Metals", "Viraj Profiles": "Metals", "Centravis": "Metals", "Aperam": "Metals",
    "Gulf Extrusions": "Metals", "Pradeep Metals": "Metals", "Peekay Steels": "Metals",
    "Bansal Wire": "Metals", "Icdas": "Metals", "Century Extrusions": "Metals",
    # automotive & auto components
    "Sundram Fasteners": "Automotive", "SKF India": "Automotive", "Bharat Forge": "Automotive",
    "Ashok Leyland": "Automotive", "TVS Motor": "Automotive", "Bosch Ltd": "Automotive",
    "Hyundai Motor India": "Automotive",
    # industrial / electrical equipment
    "Tega Industries": "Industrial Equipment", "Ensto India": "Electrical Equipment",
    "Havells India": "Electrical Equipment", "Polycab India": "Electrical Equipment",
    # building materials & cement
    "Construction Materials Industries (CMI Oman)": "Cement & Building Materials",
    "Deccan Cements": "Cement & Building Materials", "Saurashtra Cement": "Cement & Building Materials",
    "NCL Industries": "Cement & Building Materials", "Apollo Pipes": "Cement & Building Materials",
    "Action Tesa": "Cement & Building Materials", "Finolex Industries": "Cement & Building Materials",
    "HSIL (Hindware)": "Cement & Building Materials",
    # chemicals, paints, fertilisers
    "NOCIL": "Chemicals", "Tata Chemicals": "Chemicals", "Vinati Organics": "Chemicals", "SRF": "Chemicals",
    "Coromandel International": "Chemicals", "Meghmani Organics": "Chemicals", "Acutaas Chemicals": "Chemicals",
    "Aarti Industries": "Chemicals", "Anupam Rasayan": "Chemicals", "Chambal Fertilisers": "Chemicals",
    "Andhra Sugars": "Chemicals", "Nippon Paint India": "Chemicals", "Asian Paints": "Chemicals",
    "Pidilite Industries": "Chemicals",
    # paper & packaging
    "Pakka": "Paper & Packaging", "JK Paper": "Paper & Packaging", "Andhra Paper": "Paper & Packaging",
    "Emami Paper Mills": "Paper & Packaging", "Seshasayee Paper": "Paper & Packaging",
    "Satia Industries": "Paper & Packaging",
    # textiles, apparel, leather
    "Asian Fabricx": "Textiles & Apparel", "Trident": "Textiles & Apparel", "Himatsingka Seide": "Textiles & Apparel",
    "Pearl Global Industries": "Textiles & Apparel", "Mirza International": "Textiles & Apparel",
    "KPR Mill": "Textiles & Apparel", "SP Apparels": "Textiles & Apparel", "Superhouse": "Textiles & Apparel",
    # finance
    "HDFC Asset Management": "Banking & Finance", "Indian Bank": "Banking & Finance", "Bandhan Bank": "Banking & Finance",
    "Aditya Birla Capital": "Banking & Finance", "M&M Financial Services": "Banking & Finance",
    "RBL Bank": "Banking & Finance", "LIC Housing Finance": "Banking & Finance", "BRAC Bank": "Banking & Finance",
    "Bajaj Finserv": "Banking & Finance", "Tata AIG": "Banking & Finance", "Saudi Awwal Bank": "Banking & Finance",
    # consumer
    "Britannia Industries": "FMCG", "Nestle India": "FMCG", "Dabur India": "FMCG",
    # tech
    "TCS": "IT Services", "Infosys": "IT Services", "Tech Mahindra": "IT Services", "Wipro": "IT Services",
    "Tata Communications": "IT Services",
    # transport
    "TR FVLS (trfvlsl.com)": "Logistics & Shipping", "Balearia": "Logistics & Shipping",
    "Hurtigruten": "Logistics & Shipping", "DP World": "Logistics & Shipping",
    "LATAM Airlines": "Aviation", "Emirates Group": "Aviation",
    # energy, real estate, other
    "Suzlon": "Energy", "ACWA Power": "Energy", "OQ": "Oil & Gas",
    "Mahindra Lifespace": "Real Estate", "Aldar Properties": "Real Estate",
    "Hosachiguru": "Agriculture",
    "Apollo Hospitals": "Healthcare", "Zydus Lifesciences": "Healthcare",
    "YPO Bangalore": "Membership & Events",
    "Nororbis Itus": "Manufacturing", "Novorbis Itus": "Manufacturing",
    # added from the first full-table run (Oct 2026)
    "Arfin India": "Metals", "Jai Balaji Industries": "Metals", "Maharashtra Seamless": "Metals", "Ma'aden": "Metals",
    "Kirloskar Ferrous": "Metals", "ISMT": "Metals", "Everest Kanto Cylinder": "Metals",
    "Borouge": "Chemicals", "GNFC": "Chemicals", "Akzo Nobel India": "Chemicals", "Industries Qatar / QAFCO": "Chemicals",
    "DCW": "Chemicals", "SABIC Agri-Nutrients": "Chemicals", "TGV SRAAC": "Chemicals", "Fertiglobe": "Chemicals",
    "Chemfab Alkalis": "Chemicals", "UPL": "Chemicals",
    "NABARD": "Banking & Finance", "Emirates NBD": "Banking & Finance",
    "Cera Sanitaryware": "Cement & Building Materials", "Everest Industries": "Cement & Building Materials",
    "Supreme Industries": "Cement & Building Materials", "Prism Johnson": "Cement & Building Materials",
    "TVS Srichakra": "Automotive", "AWL Agri Business": "FMCG",
    "EID Parry": "Agriculture", "Harrisons Malayalam": "Agriculture", "Shree Renuka Sugars": "Agriculture",
    "Syngene International": "Healthcare", "Neuland Laboratories": "Healthcare",
}
# name keywords -> industry, for accounts not in NAME_IND yet
KEYWORD_IND = [
    (r"bank|financ|capital|insur|asset management|housing finance", "Banking & Finance"),
    (r"steel|metal|alumin|stainless|extrusion|forg|wire|tubes|ispat", "Metals"),
    (r"cement|pipes|tiles|ceramic|building", "Cement & Building Materials"),
    (r"paper|pulp|packag", "Paper & Packaging"),
    (r"chemical|organics|fertili|rasayan|paint|pharma", "Chemicals"),
    (r"textile|fabric|apparel|garment|mill\b|spinning|denim|leather|footwear", "Textiles & Apparel"),
    (r"motor|auto|tyre|tire", "Automotive"),
    (r"airline|airways|aviation", "Aviation"),
    (r"logistic|shipping|freight|port|maritime|cargo", "Logistics & Shipping"),
    (r"power|energy|solar|wind|renewabl", "Energy"),
    (r"hospital|health|life ?science", "Healthcare"),
    (r"propert|realty|estate|lifespace", "Real Estate"),
    (r"foods?\b|beverage|consumer", "FMCG"),
    (r"electric|cable", "Electrical Equipment"),
]

def industry_for(name):
    if name in NAME_IND:
        return NAME_IND[name]
    low = name.lower()
    for pat, ind in KEYWORD_IND:
        if re.search(pat, low):
            return ind
    return "Unmapped"

HOT = re.compile(r"ranked #[12]\b|\bmsa\b|\bmou\b|first real|highly motivated|first mover|strong (fit|interest)|keen", re.I)
COLD = re.compile(r"lower priority|no immediate need|low carbon-os pull|\bpark\b|then (park|close)|pushed back|"
                  r"already working with|not looking|no active project|wrong contact|who are you|not interested", re.I)
INBOUND = re.compile(r"\binbound\b|organic search|via the website|demo request", re.I)
RATING = re.compile(r"(\d+(?:\.\d+)?)\s*(?:to\s*(\d+(?:\.\d+)?)\s*)?/\s*10\b")

def temperature(note):
    """-> (label, multiplier, rating|None) from a GTM next-action note."""
    m = RATING.search(note or "")
    if m:
        r = (float(m.group(1)) + float(m.group(2))) / 2 if m.group(2) else float(m.group(1))
        mult = max(0.5, min(1.8, r / 5))
        return ("hot" if r >= 7 else "cold" if r < 5 else "warm"), round(mult, 2), r
    hot, cold = bool(HOT.search(note or "")), bool(COLD.search(note or ""))
    if hot and not cold:
        return "hot", 1.5, None
    if cold and not hot:
        return "cold", 0.5, None
    return "warm", 1.0, None

ROW = re.compile(r"\t([^\t\n]+?) · ([A-Z]{2,5})\t([A-Z][A-Z ]*?)\t([^\t\n]*)\t")
TAIL = re.compile(r"\n\t([^\t\n]*)\t([^\t\n]*)\t([^\n]*)")

def parse_motion_table(txt):
    """Rows of the Motion accounts table, from its innerText."""
    h = txt.find("ACCOUNT\tFRAMEWORK")
    if h < 0:
        return []
    body = txt[txt.find("\n", h) + 1:]
    out, pos = [], 0
    for m in ROW.finditer(body):
        head = [l.strip() for l in body[pos:m.start()].split("\n") if l.strip()]
        if not head:
            continue
        name = head[0]
        tail = TAIL.search(body, m.end())
        note = tail.group(3).strip() if tail and tail.start() - m.end() < 40 else ""
        pos = tail.end() if tail and tail.start() - m.end() < 40 else m.end()
        out.append({"name": name, "framework": m.group(1).strip(), "country": m.group(2),
                    "stage": m.group(3).strip(), "owner": m.group(4).strip(), "_note": note})
    return out

def parse_call_status(txt):
    """{account: 'HELD'|'UPCOMING'|'NO-SHOW'} from the CALLS BOOKED line."""
    m = re.search(r"CALLS BOOKED[^\n]*", txt)
    st = {}
    if m:
        line = m.group(0).split(")", 1)[-1]          # skip the "(18 · 12 HELD, …)" summary
        for s, name in re.findall(r"(UPCOMING|HELD|NO-SHOW)\s*([^·—]+?)\s+—", line):
            st[name.strip()] = s
    return st

def parse_text(txt):
    rows = parse_motion_table(txt)
    call_st = parse_call_status(txt)

    def num_after(label):
        lines = txt.split("\n")
        for i, l in enumerate(lines):
            if l.strip().lower() == label.lower() and i + 1 < len(lines):
                mm = re.search(r"\d[\d,]*", lines[i + 1])
                if mm: return int(mm.group().replace(",", ""))
        return None

    funnel = {"contacted": num_after("Contacted"), "replied": num_after("Replied"),
              "call": num_after("Call"), "proposal": num_after("Proposal"), "won": num_after("Won")}

    accounts, by = [], {}
    for r in rows:
        stage, note = r["stage"], r.pop("_note")
        temp, mult, rating = temperature(note) if stage in ENGAGED else ("", 1.0, None)
        inbound = bool(INBOUND.search(note))
        cs = call_st.get(r["name"], "") if stage == "CALL" else ""
        w = CALL_UPCOMING_W if (stage == "CALL" and cs == "UPCOMING") else STAGE_W.get(stage, 0)
        pull = round(w * mult * (1.5 if inbound else 1.0), 2)
        a = {"name": r["name"], "industry": industry_for(r["name"]), "framework": r["framework"],
             "country": r["country"], "stage": stage, "pull": pull}
        if cs: a["call"] = cs
        if inbound: a["inbound"] = True
        if temp: a["temp"] = temp
        if rating is not None: a["rating"] = rating
        accounts.append(a)
        o = by.setdefault(a["industry"], {"reached": 0, "engaged": 0, "calls": 0, "proposals": 0, "won": 0,
                                          "inbound": 0, "hot": 0, "cold": 0, "pull": 0.0})
        o["reached"] += 1
        if stage in ENGAGED: o["engaged"] += 1
        if stage in ("CALL", "PROPOSAL", "WON"): o["calls"] += 1
        if stage == "PROPOSAL": o["proposals"] += 1
        if stage == "WON": o["won"] += 1
        if inbound: o["inbound"] += 1
        if temp == "hot": o["hot"] += 1
        if temp == "cold": o["cold"] += 1
        o["pull"] += pull
    for o in by.values():
        o["pull"] = round(o["pull"], 2)
        o["pullAdj"] = round(o["pull"] / math.sqrt(o["reached"]), 3)

    snap = ""
    mm = re.search(r"\n(\d{1,2} \w+ \d{4},[^\n]*IST)", txt)
    if mm: snap = mm.group(1).strip()

    return {
        "generatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "snapshotLabel": snap,
        "source": GTM_URL,
        "model": "v2: engaged-only, depth x temperature x inbound; pullAdj = pull/sqrt(reached)",
        "funnel": funnel,
        "accountsParsed": len(accounts),
        "byIndustry": by,
        "accounts": accounts,
    }

def render_live():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.goto(GTM_URL, wait_until="networkidle", timeout=60000)
        pg.wait_for_timeout(4000)
        head = pg.inner_text("main")            # funnel + CALLS BOOKED line live on the default tab
        pg.get_by_role("button", name=re.compile(r"^Motion accounts")).click()
        pg.wait_for_timeout(1500)
        txt = pg.inner_text("main")
        b.close()
        return head + "\n" + txt

def main():
    if "--file" in sys.argv:
        txt = open(sys.argv[sys.argv.index("--file") + 1]).read()
    else:
        txt = render_live()
    data = parse_text(txt)
    if data["accountsParsed"] < 10 and "--file" not in sys.argv:
        sys.exit(f"only {data['accountsParsed']} accounts parsed — GTM layout probably changed; not overwriting gtm.json")
    out = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else "gtm.json"
    json.dump(data, open(out, "w"), indent=1)
    print(f"wrote {out}: {data['accountsParsed']} accounts across {len(data['byIndustry'])} industries")
    for ind, o in sorted(data["byIndustry"].items(), key=lambda kv: -kv[1]["pullAdj"]):
        print(f"  {ind:28} pullAdj={o['pullAdj']:6} pull={o['pull']:5} engaged={o['engaged']}/{o['reached']} inbound={o['inbound']}")
    um = [a["name"] for a in data["accounts"] if a["industry"] == "Unmapped"]
    if um:
        print("  unmapped (add to NAME_IND):", ", ".join(um))

if __name__ == "__main__":
    main()
