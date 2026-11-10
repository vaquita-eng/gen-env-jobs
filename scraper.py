"""Checks job pages daily and keeps environment-related jobs in the Geneva area.

Per site (see sites.json):
  topic:    "all"    -> keep every job link on the page
            "filter" -> open each job page and keep it only if the description
                        mentions environment keywords (see TOPIC_WORDS)
  location: "assume" -> the whole site is in the Geneva area
            "require"-> the job must mention a place from PLACES (or the site's own
                        "places" list), in the listing or on the job page
"""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent
SITES = json.loads((ROOT / "sites.json").read_text(encoding="utf-8"))
DATA_FILE = ROOT / "data" / "jobs.json"

# ---- Settings you can edit -------------------------------------------------
# Words are matched at the START of a word, in any case ("climat" matches climate,
# climatique, climatic...).
TOPIC_WORDS = [
    "environ", "sustainab", "durab", "climat", "biodiv", "ecolog", "écolog", "agroecolog",
    "conservation", "nature-based", "natural resource", "ressources naturelles",
    "protection de la nature", "carbon", "carbone", "co2", "decarbon", "décarbon",
    "emission", "émission", "greenhouse", "effet de serre", "ghg", "net zero", "fossil",
    "paris agreement", "accord de paris", "energy", "énerg", "renewable", "renouvelable",
    "solar", "solaire", "éolien", "waste", "déchet", "dechet", "recycl", "circular",
    "circulaire", "pollut", "water", "eau", "forest", "forêt", "ocean", "océan", "marine",
    "agricultur", "plastic", "plastique", "chemical", "chimique", "ozone", "resilien",
    "résilien", "disaster risk", "esg", "unep", "undrr", "unece", "onu environnement",
    "un environment",
]
PLACES = [
    "geneva", "genève", "geneve", "genf", "gland", "carouge", "meyrin", "vernier", "lancy",
    "onex", "versoix", "satigny", "cologny", "bernex", "thônex", "thonex", "plan-les-ouates",
    "grand-saconnex", "chêne-bourg", "chene-bourg", "pregny", "ferney",
]
# A job page must contain at least this many keyword matches to count as
# environment-related. 2 avoids false hits like "the nature of the work".
# Set to 1 to accept any single mention.
MIN_TOPIC_HITS = 2
# Job pages opened per site per run. Large sites are checked over several days.
MAX_DETAIL_PAGES = 60
# ----------------------------------------------------------------------------

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
SKIP = re.compile(
    r"\b(cookie|privacy|terms|login|log in|sign in|sitemap|accessibility|newsletter|"
    r"subscribe|donate|facebook|twitter|linkedin|instagram|youtube|contact us|about us|"
    r"read more|learn more|skip to|mentions légales)\b", re.I)
JOBISH = re.compile(
    r"(job|vacanc|career|position|emploi|poste|offre|recruit|requisition|opening|posting|"
    r"candidat|stage|intern|consult|manager|officer|assistant|director|specialist|analyst|"
    r"coordinator|advis|associate|engineer|head of|chargé|responsable|stagiaire|expert|"
    r"researcher|fellow|apprentice)", re.I)
TRACKING = re.compile(r"^(utm_.*|fbclid|gclid|_ga|_gl|source|jsessionid|session.*)$", re.I)

JS_LINKS = r"""() => [...document.querySelectorAll('a[href]')]
  .filter(a => !a.closest('header, nav, footer'))
  .map(a => {
    const clean = s => (s || '').replace(/\s+/g, ' ').trim();
    const row = a.closest('li, tr, article, [role=row], [class*=job], [class*=card]');
    return {text: clean(a.innerText || a.getAttribute('aria-label')),
            href: a.href, ctx: clean(row ? row.innerText : '').slice(0, 400)};
  })"""

# Text of a job page without menus/footers (which repeat the organisation's mission).
JS_BODY = r"""() => {
  const root = document.querySelector('main, [role=main]') || document.body;
  const c = root.cloneNode(true);
  c.querySelectorAll('header, nav, footer, script, style, noscript').forEach(e => e.remove());
  return (c.textContent || '').replace(/\s+/g, ' ').slice(0, 20000);
}"""


def words_regex(words):
    return re.compile(r"(?<!\w)(?:%s)" % "|".join(re.escape(w) for w in words), re.I)


TOPIC = words_regex(TOPIC_WORDS)


def norm_url(url):
    p = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING.match(k)]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), ""))


def load(page, url):
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    for _ in range(4):  # scroll to trigger lazy-loaded lists
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(700)


def candidates(page, site):
    load(page, site["url"])
    base = norm_url(site["url"])
    seen, out = set(), []
    for link in page.evaluate(JS_LINKS):
        text, href = link["text"], link["href"]
        if not (8 <= len(text) <= 200) or not href.startswith("http") or SKIP.search(text):
            continue
        url = norm_url(href)
        if url == base or url in seen:
            continue
        # On "filter" sites we open each job page, so first drop links that
        # clearly aren't jobs (saves time).
        if site["topic"] == "filter" and not JOBISH.search(text + " " + href):
            continue
        seen.add(url)
        out.append({"title": text, "url": url, "ctx": link["ctx"]})
    return out


def read_page(page, url):
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=6000)
        except Exception:
            pass
        page.wait_for_timeout(1500)
        return page.evaluate(JS_BODY)
    except Exception:
        return None  # couldn't read it; try again next run


def main():
    state = json.loads(DATA_FILE.read_text(encoding="utf-8")) if DATA_FILE.exists() else {}
    jobs = {j["id"]: j for j in state.get("jobs", [])}
    checked = state.get("checked", {})
    old_sites = state.get("sites", {})
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    sites_status = {}

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(user_agent=UA, locale="en-GB")
        for site in SITES:
            name = site["name"]
            known = old_sites.get(name, {}).get("baseline_done", False)
            need_topic = site["topic"] == "filter"
            need_place = site.get("location", "require") == "require"
            page = context.new_page()
            try:
                cands = candidates(page, site)
                if not cands:
                    raise RuntimeError("No job links found. The page may load jobs differently.")
                places = words_regex(site.get("places", PLACES))
                kept, fetched, skipped = [], 0, 0
                for c in cands:
                    listing = c["title"] + " " + c["ctx"]
                    topic_ok = (not need_topic) or bool(TOPIC.search(listing))
                    place_ok = (not need_place) or bool(places.search(listing))
                    if not (topic_ok and place_ok):
                        cached = checked.get(c["url"])
                        if isinstance(cached, dict):
                            topic_ok = topic_ok or cached["topic"]
                            place_ok = place_ok or cached["place"]
                        elif fetched < MAX_DETAIL_PAGES:
                            fetched += 1
                            body = read_page(page, c["url"])
                            if body is None:
                                skipped += 1
                                continue
                            topic_ok = topic_ok or len(TOPIC.findall(body)) >= MIN_TOPIC_HITS
                            place_ok = place_ok or bool(places.search(body))
                            checked[c["url"]] = {"topic": topic_ok, "place": place_ok}
                        else:
                            skipped += 1
                            continue
                    if topic_ok and place_ok:
                        kept.append(c)

                kept_ids = set()
                for c in kept:
                    jid = hashlib.sha1((name + c["url"]).encode()).hexdigest()[:12]
                    kept_ids.add(jid)
                    if jid in jobs:
                        jobs[jid].update(title=c["title"], last_seen=today, active=True)
                    else:
                        jobs[jid] = {"id": jid, "site": name, "title": c["title"], "url": c["url"],
                                     "first_seen": today, "last_seen": today, "active": True,
                                     "baseline": not known}
                for j in jobs.values():
                    if j["site"] == name and j["id"] not in kept_ids:
                        j["active"] = False
                sites_status[name] = {"ok": True, "count": len(kept), "pending": skipped,
                                      "baseline_done": known or skipped == 0}
                print(f"OK    {name}: {len(kept)} kept of {len(cands)} links, {skipped} pending")
            except Exception as e:
                sites_status[name] = {"ok": False, "message": str(e).splitlines()[0][:160],
                                      "baseline_done": known}
                print(f"FAIL  {name}: {e}")
            finally:
                page.close()
        browser.close()

    cutoff = (now - timedelta(days=60)).date().isoformat()
    kept_jobs = [j for j in jobs.values() if j["active"] or j["last_seen"] >= cutoff]
    kept_jobs.sort(key=lambda j: (j["first_seen"], j["site"]), reverse=True)
    DATA_FILE.parent.mkdir(exist_ok=True)
    DATA_FILE.write_text(json.dumps(
        {"updated": now.isoformat(timespec="seconds"), "sites": sites_status,
         "jobs": kept_jobs, "checked": checked}, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
