"""Internship agent for the Summer 2027 MBA internship feed.

Each run:
  1. Pulls current postings from live job feeds (Amazon Jobs, Workday career
     sites, and a community-maintained MBA internship list).
  2. Checks them against its memory in data.json. Only roles it has never seen
     get the expensive work: fetching the full posting and classifying it.
  3. Marks roles that disappeared from a feed as closed, and forgets them
     after CLOSED_KEEP_DAYS.
  4. Merges in the hand-curated roles from curated.json (MBB, China portals).
  5. Writes everything back to data.json, which the website reads.

If PORTKEY_API_KEY is set (via .env locally or a GitHub Actions secret), new
roles are also classified by gpt-6-luna through Portkey. Without the key the
agent falls back to keyword rules.

Run locally:  .venv/bin/python scripts/agent.py
"""

import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data.json"
CURATED_PATH = ROOT / "curated.json"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/128 Safari/537.36"
CLOSED_KEEP_DAYS = 14
IGNORED_KEEP_DAYS = 30
NEW_DAYS = 7
MODEL = "gpt-6-luna"

try:  # python.org builds on macOS ship without CA certificates
    import certifi
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CONTEXT = ssl.create_default_context(cafile="/etc/ssl/cert.pem" if Path("/etc/ssl/cert.pem").exists() else None)

# Workday career sites: (company, host, tenant, site, search queries)
WORKDAY_SITES = [
    ("Amgen", "amgen.wd1", "amgen", "Careers", ["MBA"]),
    ("Adobe", "adobe.wd5", "adobe", "external_experienced", ["MBA"]),
    ("Johnson & Johnson", "jj.wd5", "jj", "JJ", ["MBA 2027", "Leadership Development Program 2027"]),
    ("Marsh McLennan", "mmc.wd1", "mmc", "MMC", ["MBA Summer Associate", "MBA 2027"]),
    ("HP", "hp.wd5", "hp", "ExternalCareerSite", ["MBA intern", "Strategy Intern"]),
    ("General Motors", "generalmotors.wd5", "generalmotors", "Careers_GM", ["MBA 2027"]),
]
AMAZON_QUERIES = ["MBA 2027", "2027 MBA internship", "Leadership Development Program 2027 intern", "MBA intern China"]
COMMUNITY_URL = "https://raw.githubusercontent.com/TrevorDohm/MBA-Tech-Internships-2027/main/README.md"
COMMUNITY_SECTIONS = {"PM": "Other", "STRATEGY": "Strategy", "PMM": "Other", "FINANCE": "Other"}

INTERNISH = re.compile(r"intern|summer associate|summer consultant|summer \d{4}|summer analyst", re.I)
MBAISH = re.compile(r"\bmba\b|leadership development|\bldp\b|summer associate|summer consultant", re.I)
FULLTIME = re.compile(r"full[- ]?time|co-?op\b", re.I)
CHINA = re.compile(r"china|shanghai|beijing|shenzhen|guangzhou|hangzhou|hong kong|\bchn\b", re.I)
US_STATES = ("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY "
             "NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY").split()
US = re.compile(r"(?i:united states|remote|new york|san francisco|seattle)|\bU\.?S\.?A?\b|, (" + "|".join(US_STATES) + r")\b")

VISA_NO = [
    re.compile(r"(must not|will not|does not|do not|cannot|can ?not|unable to|won't|not able to|not eligible|no)\s[^.]{0,80}?sponsor", re.I),
    re.compile(r"without[^.]{0,50}sponsorship", re.I),
    re.compile(r"permanent(ly)?\s+authori[sz]ed", re.I),
]
VISA_YES = [
    re.compile(r"(will|can|may|able to|is able to)\s+(offer|provide|consider)\s+[^.]{0,30}?sponsorship", re.I),
    re.compile(r"sponsorship (is )?available", re.I),
    re.compile(r"hires international students", re.I),
]


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    print(msg, flush=True)


def http(url, data=None, timeout=25):
    headers = {"User-Agent": UA, "Accept": "application/json, text/plain, */*"}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as resp:
        return resp.read().decode("utf-8", "replace")


def http_json(url, data=None):
    return json.loads(http(url, data))


def norm_url(url):
    p = urllib.parse.urlsplit(url.strip())
    host = p.netloc.lower().removeprefix("www.")
    path = re.sub(r"/en-US(?=/)", "", p.path).rstrip("/")
    return f"{host}{path}".lower()


def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", s).strip()


def region_of(text):
    if CHINA.search(text or ""):
        return "China"
    if US.search(text or ""):
        return "US"
    return None


def type_of(company, title, default="Other"):
    t = f"{company} {title}".lower()
    if re.search(r"consult|oliver wyman|summer associate", t) and "product" not in t:
        return "Consulting"
    if re.search(r"leadership|development program|\bldp\b|fldp|mldp|rotation|accelerator|pathways", t):
        return "LDP"
    if re.search(r"strategy|bizops|business operations|corporate development|corp dev", t):
        return "Strategy"
    return default


def visa_from_text(text):
    for rx in VISA_NO:
        if rx.search(text or ""):
            return "no"
    for rx in VISA_YES:
        if rx.search(text or ""):
            return "yes"
    return "ask"


def parse_date(s):
    """Best-effort parse of the many date formats job sites use. Returns YYYY-MM-DD or None."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    m = re.match(r"([A-Za-z]{3}) (\d{1,2})$", s)  # "Oct 01" from the community list
    if m:
        today = date.today()
        try:
            d = datetime.strptime(f"{m.group(1)} {m.group(2)} {today.year}", "%b %d %Y").date()
        except ValueError:
            return None
        if d > today + timedelta(days=1):
            d = d.replace(year=today.year - 1)
        return d.isoformat()
    m = re.search(r"posted (\d+)\+? days? ago", s, re.I)
    if m:
        return (date.today() - timedelta(days=int(m.group(1)))).isoformat()
    if re.search(r"posted (today|yesterday)", s, re.I):
        return (date.today() - timedelta(days=0 if "today" in s.lower() else 1)).isoformat()
    return None


def pretty_date(d):
    return datetime.strptime(d, "%Y-%m-%d").strftime("%b %d, %Y").replace(" 0", " ")


# ---------------------------------------------------------------- AI classifier

class Classifier:
    """Optional gpt-6-luna classifier. Only called for roles the agent hasn't seen before."""

    def __init__(self):
        self.client = None
        self.calls = 0
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass
        key = os.environ.get("PORTKEY_API_KEY")
        if not key:
            log("AI classifier: off (no PORTKEY_API_KEY). Using keyword rules.")
            return
        try:
            from openai import OpenAI
            self.client = OpenAI(
                api_key=key,
                base_url="https://api.portkey.ai/v1",
                default_headers={"x-portkey-api-key": key},
            )
            log(f"AI classifier: on ({MODEL} via Portkey).")
        except Exception as e:
            log(f"AI classifier: off ({e.__class__.__name__}). Using keyword rules.")

    def classify(self, company, title, location, description):
        if not self.client:
            return None
        prompt = (
            "You screen job postings for a first-year MBA student looking for a Summer 2027 internship "
            "in consulting, leadership development programs (LDP), or corporate strategy.\n"
            "Return ONLY a JSON object with these keys:\n"
            '  "relevant": true if this is a Summer 2027 internship open to MBA students, else false\n'
            '  "type": one of "Consulting", "LDP", "Strategy", "Other"\n'
            '  "visa": "yes" if the posting explicitly offers visa sponsorship, "no" if it explicitly rules '
            'sponsorship out or requires permanent work authorization, otherwise "ask"\n'
            '  "note": at most 30 words of facts a candidate needs (dates, pay, eligibility, location). No hype.\n\n'
            f"Company: {company}\nTitle: {title}\nLocation: {location}\n"
            f"Posting text:\n{description[:6000]}"
        )
        try:
            self.calls += 1
            resp = self.client.responses.create(model=MODEL, input=prompt)
            text = resp.output_text.strip()
            text = re.sub(r"^```(json)?|```$", "", text, flags=re.M).strip()
            out = json.loads(text)
            if out.get("type") not in ("Consulting", "LDP", "Strategy", "Other"):
                out["type"] = None
            if out.get("visa") not in ("yes", "no", "ask"):
                out["visa"] = "ask"
            return out
        except Exception as e:
            log(f"  AI classify failed for {company} / {title}: {e.__class__.__name__}")
            return None


# ---------------------------------------------------------------- sources
# Each source returns a list of "stubs": cheap listing data plus an enrich()
# callable that does the expensive per-role work. The agent only calls enrich()
# for roles that aren't in memory yet.

def source_amazon():
    seen, stubs = set(), []
    for q in AMAZON_QUERIES:
        url = "https://www.amazon.jobs/en/search.json?" + urllib.parse.urlencode({"base_query": q, "result_limit": 100})
        for j in http_json(url).get("jobs", []):
            title = j.get("title", "").strip()
            if j["id"] in seen or not INTERNISH.search(title) or FULLTIME.search(title) or not MBAISH.search(title):
                continue
            seen.add(j["id"])
            cc = j.get("country_code")
            region = "US" if cc == "USA" else "China" if cc in ("CHN", "HKG") else None
            if not region:
                continue
            link = "https://www.amazon.jobs" + j["job_path"]
            text = strip_html(" ".join(j.get(k) or "" for k in ("description", "basic_qualifications", "preferred_qualifications")))

            def enrich(j=j, title=title, region=region, link=link, text=text):
                return {
                    "company": "Amazon", "role": title, "region": region,
                    "location": j.get("normalized_location") or j.get("location") or "",
                    "deadline": "Rolling (apply early)", "deadlineDate": None,
                    "posted": parse_date(j.get("posted_date")), "url": link,
                    "visa": visa_from_text(text), "_text": text,
                }
            stubs.append({"id": norm_url(link), "url": link, "enrich": enrich})
    return stubs


def source_workday():
    stubs, seen = [], set()
    for company, host, tenant, site, queries in WORKDAY_SITES:
        api = f"https://{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
        for q in queries:
            offset, total = 0, 1
            while offset < min(total, 100):
                page = http_json(f"{api}/jobs", {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": q})
                total = page.get("total") or 0
                for p in page.get("jobPostings", []):
                    title, path = p.get("title", ""), p.get("externalPath", "")
                    link = f"https://{host}.myworkdayjobs.com/{site}{path}"
                    rid = norm_url(link)
                    if rid in seen or not INTERNISH.search(title) or FULLTIME.search(title):
                        continue
                    if not MBAISH.search(title) and company != "Marsh McLennan":
                        continue
                    seen.add(rid)

                    def enrich(company=company, api=api, path=path, title=title, link=link, posted=p.get("postedOn")):
                        info = http_json(f"{api}{path}").get("jobPostingInfo", {})
                        text = strip_html(info.get("jobDescription"))
                        locs = [info.get("location", "")] + (info.get("additionalLocations") or [])
                        country = (info.get("country") or {}).get("descriptor", "")
                        region = region_of(country) or region_of(" ".join(locs))
                        if region is None:
                            return None
                        name = company
                        if company == "Marsh McLennan":
                            name = "Oliver Wyman" if title.lower().startswith("oliver wyman") else "Marsh McLennan (Mercer)"
                        end = info.get("endDate")
                        short_locs = [re.sub(r",\s*United States of America$", "", l) for l in locs if l]
                        return {
                            "company": name, "role": re.sub(r"^Oliver Wyman\s*-\s*", "", title), "region": region,
                            "location": " · ".join(short_locs[:4]) + (f" +{len(short_locs) - 4} more" if len(short_locs) > 4 else ""),
                            "deadline": pretty_date(end) if end else "Rolling", "deadlineDate": end,
                            "posted": info.get("startDate") or parse_date(posted), "url": link,
                            "visa": visa_from_text(text), "_text": text,
                            "_type": "Consulting" if company == "Marsh McLennan" else None,
                        }
                    stubs.append({"id": rid, "url": link, "enrich": enrich})
                offset += 20
    return stubs


def source_community():
    """Parse the community-maintained MBA tech internship list (markdown tables)."""
    md = http(COMMUNITY_URL)
    stubs = []
    for section, default_type in COMMUNITY_SECTIONS.items():
        m = re.search(rf"<!-- TABLE_START:{section} -->(.*?)<!-- TABLE_END:{section} -->", md, re.S)
        if not m:
            continue
        company = ""
        for line in m.group(1).splitlines():
            if not line.startswith("|") or line.startswith("| Company") or line.startswith("| ---"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 8:
                continue
            comp, role, loc, app, pay, visa, posted, notes = cells[:8]
            comp = comp.replace("*", "").strip()
            company = company if comp in ("↳", "") else comp
            if "🔒" in app:
                continue
            link = re.search(r"\((https?://[^)\s]+)\)", app)
            if not link:
                continue
            loc = re.sub(r"</?br\s*/?>", " · ", loc, flags=re.I)
            region = region_of(loc)
            if region is None:
                continue
            expected = "⏳" in app
            note = re.sub(r"\s+", " ", notes)
            if pay and pay != "—":
                note = f"Pay: {pay}. {note}"
            item = {
                "company": company, "role": role, "region": region, "location": loc,
                "deadline": "Not yet posted" if expected else "Open now", "deadlineDate": None,
                "posted": None if expected else parse_date(posted), "url": link.group(1),
                "visa": "yes" if "✅" in visa else "no" if "🚫" in visa else "ask",
                "type": type_of(company, role, default_type),
                "note": note[:260] + ("…" if len(note) > 260 else ""),
            }
            stubs.append({"id": norm_url(item["url"]), "url": item["url"], "enrich": (lambda item=item: item), "cheap": True})
    return stubs


SOURCES = [("Amazon Jobs", source_amazon), ("Workday", source_workday), ("Community list", source_community)]


# ---------------------------------------------------------------- agent loop

def load_memory():
    if DATA_PATH.exists():
        try:
            return json.loads(DATA_PATH.read_text())
        except json.JSONDecodeError:
            log("data.json was unreadable; starting with empty memory.")
    return {"created_at": iso(now_utc()), "roles": [], "ignored": {}, "runs": []}


def finish_role(role, ai, source):
    """Fill in type/visa/note using AI output when available, rules otherwise."""
    text = role.pop("_text", "")
    forced_type = role.pop("_type", None)
    if ai:
        if ai.get("relevant") is False:
            return None
        role["type"] = forced_type or ai.get("type") or type_of(role["company"], role["role"])
        if role.get("visa") == "ask":  # explicit posting language (rules) wins over the model
            role["visa"] = ai.get("visa", "ask")
        role["note"] = (ai.get("note") or "").strip()
    else:
        role["type"] = forced_type or role.get("type") or type_of(role["company"], role["role"])
        role.setdefault("note", "")
        if not role["note"] and text:
            m = re.search(r"[^.]*\b(weeks?|pay|salary|\$)[^.]*\.", text, re.I)
            role["note"] = (m.group(0).strip()[:220] if m else "")
    role["source"] = source
    return role


def run():
    started = now_utc()
    today = date.today()
    mem = load_memory()
    first_run = not mem.get("roles")
    by_id = {r["id"]: r for r in mem.get("roles", []) if r.get("source") != "curated"}
    ignored = mem.get("ignored", {})
    ai = Classifier()
    stats = {"new": 0, "closed": 0, "reopened": 0, "skipped_known": 0, "sources": {}}

    for name, fn in SOURCES:
        try:
            stubs = fn()
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as e:
            log(f"[{name}] FAILED: {e}. Keeping its roles from memory unchanged.")
            stats["sources"][name] = f"error: {e.__class__.__name__}"
            continue
        seen_ids = set()
        added = 0
        for stub in stubs:
            rid = stub["id"]
            seen_ids.add(rid)
            if rid in ignored:
                ignored[rid] = iso(started)
                continue
            known = by_id.get(rid)
            if known and known.get("source") != name:
                # Same posting already tracked by a richer feed. Borrow the list's visa flag if ours is unknown.
                if stub.get("cheap") and known.get("visa") == "ask":
                    known["visa"] = stub["enrich"]().get("visa", "ask")
                continue
            if known and not stub.get("cheap"):
                # Memory hit: no detail fetch, no AI call.
                known["last_seen"] = iso(started)
                if known.get("status") == "closed":
                    known["status"] = "open"
                    known.pop("closed_at", None)
                    stats["reopened"] += 1
                stats["skipped_known"] += 1
                continue
            try:
                role = stub["enrich"]()
            except Exception as e:
                log(f"  [{name}] could not fetch {stub['url']}: {e.__class__.__name__}")
                continue
            if role is None:
                ignored[rid] = iso(started)
                continue
            if known:  # cheap source: refresh fields, keep memory timestamps
                stats["skipped_known"] += 1
                role.update({k: known[k] for k in ("first_seen",) if k in known})
                role["source"] = name
                role["type"] = role.get("type") or known.get("type")
            else:
                text = role.get("_text", "")
                verdict = ai.classify(role["company"], role["role"], role["location"], text) if text else None
                role = finish_role(role, verdict, name)
                if role is None:
                    ignored[rid] = iso(started)
                    continue
                role["first_seen"] = iso(started)
                added += 1
                stats["new"] += 1
                log(f"  + NEW [{name}] {role['company']}: {role['role']} ({role['region']}, visa={role['visa']})")
            role.pop("_text", None)
            role.pop("_type", None)
            role.update({"id": rid, "status": "open", "last_seen": iso(started)})
            by_id[rid] = role
        # Roles from this source that vanished from the feed are closed.
        for r in by_id.values():
            if r.get("source") == name and r["id"] not in seen_ids and r.get("status") == "open":
                r["status"] = "closed"
                r["closed_at"] = iso(started)
                stats["closed"] += 1
                log(f"  - CLOSED [{name}] {r['company']}: {r['role']}")
        stats["sources"][name] = f"{len(seen_ids)} listed, {added} new"
        log(f"[{name}] {len(seen_ids)} listed, {added} new")

    # Forget roles closed long ago and ignore-list entries not seen in a while.
    cutoff = iso(started - timedelta(days=CLOSED_KEEP_DAYS))
    by_id = {k: r for k, r in by_id.items() if not (r.get("status") == "closed" and r.get("closed_at", "") < cutoff)}
    ign_cutoff = iso(started - timedelta(days=IGNORED_KEEP_DAYS))
    ignored = {k: v for k, v in ignored.items() if v >= ign_cutoff}

    # Live roles win over curated duplicates.
    curated = json.loads(CURATED_PATH.read_text())
    old_curated = {r["id"]: r for r in mem.get("roles", []) if r.get("source") == "curated"}
    for c in curated:
        cid = norm_url(c["url"]) + "#" + re.sub(r"\W+", "-", c["company"].lower())
        if norm_url(c["url"]) in by_id:
            continue
        prev = old_curated.get(cid, {})
        by_id[cid] = {**c, "id": cid, "source": "curated", "status": "open",
                      "first_seen": prev.get("first_seen", iso(started)), "last_seen": iso(started)}

    # "New" = posted in the last week, or first spotted in the last week (after the first run).
    new_cutoff = (today - timedelta(days=NEW_DAYS)).isoformat()
    for r in by_id.values():
        posted_recent = bool(r.get("posted")) and r["posted"] >= new_cutoff
        spotted_recent = not first_run and r.get("first_seen", "") >= iso(started - timedelta(days=NEW_DAYS)) \
            and r.get("first_seen", "") > mem.get("created_at", "")
        r["new"] = r.get("status") == "open" and (posted_recent or spotted_recent)

    roles = sorted(by_id.values(), key=lambda r: (r.get("status") != "open", r.get("region"), r.get("type"), r.get("company"), r.get("role")))
    run_log = {"at": iso(started), "seconds": round((now_utc() - started).total_seconds(), 1), "ai_calls": ai.calls, **stats}
    out = {
        "updated_at": iso(started),
        "created_at": mem.get("created_at", iso(started)),
        "refresh_hours": 6,
        "roles": roles,
        "ignored": ignored,
        "runs": ([run_log] + mem.get("runs", []))[:30],
    }
    DATA_PATH.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    open_count = sum(r["status"] == "open" for r in roles)
    log(f"Done: {open_count} open roles, {stats['new']} new, {stats['closed']} closed, "
        f"{stats['skipped_known']} remembered (no re-fetch), {ai.calls} AI calls, {run_log['seconds']}s.")


if __name__ == "__main__":
    sys.exit(run())
