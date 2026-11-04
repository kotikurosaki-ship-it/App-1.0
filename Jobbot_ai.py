#!/usr/bin/env python3
"""
JobBot AI - intent understanding, live multi-provider job search, semantic
ranking and scam judgement. Degrades to heuristics without an API key.
"""

import re
import json
import hashlib
import datetime
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed

import jobbot_core as core

UA = "Mozilla/5.0 (Android) JobBot/2.0"


def _http(url, data=None, headers=None, method=None, timeout=25):
    h = {"User-Agent": UA, "Accept": "application/json"}
    h.update(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=h,
                                 method=method or ("POST" if body else "GET"))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")


def _json(url, **kw):
    return json.loads(_http(url, **kw))


def _strip_html(s):
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s or "", flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</p>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
          .replace("&nbsp;", " ").replace("&#39;", "'").replace("&quot;", '"'))
    return re.sub(r"[ \t]+", " ", s).strip()


# ==========================================================
# AI CLIENT - any OpenAI-compatible endpoint
# ==========================================================
class AIClient:
    def __init__(self, settings=None):
        s = settings or core.load_settings()
        self.enabled = bool(s.get("ai_enabled", True))
        self.key = (s.get("ai_api_key") or "").strip()
        self.base = (s.get("ai_base_url") or "").rstrip("/")
        self.model = s.get("ai_model") or "gpt-4o-mini"

    def available(self):
        return bool(self.enabled and self.key and self.base)

    def chat(self, system, user, json_mode=True, max_tokens=1600, temperature=0.3):
        if not self.available():
            raise RuntimeError("AI not configured")
        payload = {"model": self.model, "temperature": temperature,
                   "max_tokens": max_tokens,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}]}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        raw = _http(f"{self.base}/chat/completions", data=payload,
                    headers={"Authorization": f"Bearer {self.key}"}, timeout=60)
        content = json.loads(raw)["choices"][0]["message"]["content"]
        if not json_mode:
            return content
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", content, re.S)
            if not m:
                raise
            return json.loads(m.group(0))

    def build_search_plan(self, user_request, province, master_cv):
        prof = _profile_digest(master_cv)
        sys_p = ("You are a South African recruitment research assistant. Turn a "
                 "jobseeker's request and CV into a concrete web-search plan. "
                 "Think about the real job titles employers advertise, not the words "
                 "the jobseeker used. Reply with JSON only, using exactly these keys: "
                 '{"queries":[],"titles":[],"locations":[],"must_have":[],'
                 '"nice_to_have":[],"exclude":[],"reasoning":""}. '
                 "queries: 5-8 short search strings an aggregator would understand. "
                 "locations: SA towns/cities inside the stated province. "
                 "exclude: words that signal the wrong job or a scam.")
        usr = (f"Province: {province or 'anywhere in South Africa'}\n"
               f"Request: {user_request or '(no specific request - use the CV)'}\n"
               f"CV summary:\n{prof}")
        plan = self.chat(sys_p, usr, max_tokens=900)
        for k in ("queries", "titles", "locations", "must_have", "nice_to_have", "exclude"):
            plan.setdefault(k, [])
        return plan

    def rank_jobs(self, jobs, master_cv, plan, batch=12):
        prof = _profile_digest(master_cv)
        wanted = ", ".join((plan or {}).get("titles") or []) or "see CV"
        sys_p = ("You screen job adverts for one specific candidate. For each advert "
                 "return a fit score 0-100, a scam risk 0-100, one short plain-English "
                 "reason (max 20 words), and any red flags. Be strict about scams: "
                 "upfront fees, WhatsApp/Telegram-only contact, vague company, "
                 "unrealistic pay, personal-document requests. "
                 'Reply JSON only: {"results":[{"uid":"","fit":0,"scam":0,'
                 '"reason":"","flags":[]}]}')
        out = {}
        for i in range(0, len(jobs), batch):
            chunk = jobs[i:i + batch]
            listing = "\n\n".join(
                f"UID:{j['uid']}\nTITLE:{j.get('title','')}\n"
                f"COMPANY:{j.get('company','')}\nLOCATION:{j.get('location','')}\n"
                f"TEXT:{(j.get('description') or '')[:900]}" for j in chunk)
            usr = (f"Candidate profile:\n{prof}\n\nTarget roles: {wanted}\n\n"
                   f"Adverts:\n{listing}")
            try:
                res = self.chat(sys_p, usr, max_tokens=1800)
                for r in res.get("results", []):
                    if r.get("uid"):
                        out[r["uid"]] = r
            except Exception as e:
                core.log(f"rank batch failed: {e}")
        return out

    def write_email(self, job, master_cv, mode):
        analysis = core.analyze_job(f"{job.get('title','')} {job.get('company','')} {job.get('description','')}")
        match = core.build_match_report(core.normalize_master_cv(dict(master_cv)), analysis)
        contact = (master_cv.get("contact") or {})
        sys_p = ("You write short, honest South African job application emails. "
                 "HARD RULE: never claim a qualification, licence, certificate or "
                 "experience that is not in the EVIDENCE list. If something is only "
                 "'transferable', say so honestly. Plain professional English, no "
                 "flattery, no buzzwords, 150-220 words, 3-4 short paragraphs. "
                 'Reply JSON only: {"subject":"","body":""}. The body must end with '
                 "a sign-off, the candidate's name, phone and email on separate lines.")
        usr = json.dumps({
            "job": {"title": job.get("title"), "company": job.get("company"),
                    "location": job.get("location"),
                    "advert": (job.get("description") or "")[:1500],
                    "link": job.get("url")},
            "candidate": {"name": contact.get("name"), "phone": contact.get("phone"),
                          "email": contact.get("email"),
                          "location": contact.get("location"),
                          "profile": master_cv.get("profile"),
                          "skills": (master_cv.get("skills") or [])[:20]},
            "EVIDENCE_direct": match.get("direct", [])[:15],
            "EVIDENCE_transferable": match.get("transferable", [])[:10],
            "attachment": ("the candidate's own CV" if mode == core.CV_MODE_OWN
                           else "a CV tailored to this advert"),
        }, ensure_ascii=False)
        r = self.chat(sys_p, usr, max_tokens=900, temperature=0.6)
        subject = (r.get("subject") or "").strip()[:180]
        body = (r.get("body") or "").strip()
        if not body:
            raise RuntimeError("empty AI letter")
        return subject, body + "\n"


def _profile_digest(master_cv):
    m = core.normalize_master_cv(dict(master_cv or {}))
    c = m.get("contact") or {}
    roles = []
    for r in (m.get("experience") or [])[:5]:
        roles.append(f"- {r.get('header','')}: {'; '.join((r.get('bullets') or [])[:3])}")
    return (f"Name: {c.get('name','')}\nBased: {c.get('location','')}\n"
            f"Summary: {(m.get('profile') or '')[:400]}\n"
            f"Skills: {', '.join((m.get('skills') or [])[:25])}\n"
            f"Education: {'; '.join((m.get('education') or [])[:4])}\n"
            f"Certificates: {'; '.join((m.get('certifications') or [])[:5])}\n"
            f"Experience:\n" + "\n".join(roles))


# ==========================================================
# LIVE JOB PROVIDERS
# ==========================================================
def _mk(uid_seed, title, company, location, url, desc, provider, salary="", posted=""):
    return {"uid": hashlib.md5(uid_seed.encode("utf-8")).hexdigest()[:12],
            "title": (title or "Untitled").strip()[:200],
            "company": (company or "").strip()[:120],
            "location": (location or "").strip()[:120],
            "url": (url or "").strip(),
            "description": _strip_html(desc)[:6000],
            "provider": provider, "salary": salary, "posted": posted}


def search_adzuna(s, query, where, limit=30):
    if not (s.get("adzuna_app_id") and s.get("adzuna_app_key")):
        return []
    country = s.get("adzuna_country") or "za"
    qs = urllib.parse.urlencode({
        "app_id": s["adzuna_app_id"], "app_key": s["adzuna_app_key"],
        "results_per_page": min(limit, 50), "what": query,
        "where": where or "", "content-type": "application/json",
        "max_days_old": 30, "sort_by": "date"})
    data = _json(f"https://api.adzuna.com/v1/api/jobs/{country}/search/1?{qs}")
    out = []
    for r in data.get("results", []):
        out.append(_mk(f"adzuna{r.get('id')}", r.get("title"),
                       ((r.get("company") or {}).get("display_name")),
                       ((r.get("location") or {}).get("display_name")),
                       r.get("redirect_url"), r.get("description"), "adzuna",
                       salary=str(r.get("salary_min") or ""),
                       posted=r.get("created") or ""))
    return out


def search_jooble(s, query, where, limit=30):
    key = (s.get("jooble_key") or "").strip()
    if not key:
        return []
    domain = s.get("jooble_domain") or "za.jooble.org"
    data = _json(f"https://{domain}/api/{key}",
                 data={"keywords": query, "location": where or "South Africa",
                       "page": "1", "ResultOnPage": min(limit, 50)})
    out = []
    for r in (data.get("jobs") or []):
        out.append(_mk(f"jooble{r.get('id')}", r.get("title"), r.get("company"),
                       r.get("location"), r.get("link"), r.get("snippet"),
                       "jooble", salary=r.get("salary") or "",
                       posted=r.get("updated") or ""))
    return out


def search_jsearch(s, query, where, limit=30):
    key = (s.get("rapidapi_key") or "").strip()
    if not key:
        return []
    q = f"{query} in {where}" if where else query
    qs = urllib.parse.urlencode({"query": q, "page": "1", "num_pages": "1",
                                 "country": "za", "date_posted": "month"})
    data = _json(f"https://jsearch.p.rapidapi.com/search?{qs}",
                 headers={"X-RapidAPI-Key": key,
                          "X-RapidAPI-Host": "jsearch.p.rapidapi.com"})
    out = []
    for r in (data.get("data") or [])[:limit]:
        loc = ", ".join(x for x in (r.get("job_city"), r.get("job_state")) if x)
        out.append(_mk(f"jsearch{r.get('job_id')}", r.get("job_title"),
                       r.get("employer_name"), loc,
                       r.get("job_apply_link") or r.get("job_google_link"),
                       r.get("job_description"), "jsearch",
                       posted=r.get("job_posted_at_datetime_utc") or ""))
        if r.get("employer_email"):
            out[-1]["contact_email"] = r["employer_email"]
    return out


def search_serpapi(s, query, where, limit=20):
    key = (s.get("serpapi_key") or "").strip()
    if not key:
        return []
    qs = urllib.parse.urlencode({"engine": "google_jobs", "q": query,
                                 "location": where or "South Africa",
                                 "hl": "en", "api_key": key})
    data = _json(f"https://serpapi.com/search?{qs}")
    out = []
    for r in (data.get("jobs_results") or [])[:limit]:
        out.append(_mk(f"serp{r.get('job_id')}", r.get("title"),
                       r.get("company_name"), r.get("location"),
                       (r.get("related_links") or [{}])[0].get("link", ""),
                       r.get("description"), "google_jobs"))
    return out


def search_rss(url, limit=60):
    out = []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            root = ET.fromstring(r.read())
        for item in list(root.iter("item"))[:limit]:
            t = item.findtext("title", "")
            link = item.findtext("link", "")
            desc = item.findtext("description", "")
            out.append(_mk(t + link, t, "", "", link, desc, "rss",
                           posted=item.findtext("pubDate", "")))
    except Exception as e:
        core.log(f"rss {url}: {e}")
    return out


# ==========================================================
# THE AGENT LOOP
# ==========================================================
def _dedupe(jobs):
    seen, out = {}, []
    for j in jobs:
        key = re.sub(r"[^a-z0-9]", "", (j["title"] + j.get("company", "")).lower())[:60]
        if key in seen:
            if len(j["description"]) > len(seen[key]["description"]):
                seen[key].update(j)
            continue
        seen[key] = j
        out.append(j)
    return out


def ai_job_search(user_request="", province="", progress=None, cancel=None, max_results=120):
    """Full agentic search. Returns the number of new jobs stored.
    Run this on a worker thread."""
    say = progress or (lambda m, **k: None)
    stop = (lambda: cancel is not None and cancel.is_set())
    s = core.load_settings()
    ai = AIClient(s)
    master = core.load_master_cv() or {}
    province = (province or s.get("province") or "").lower()

    plan = None
    if ai.available() and master:
        try:
            say("Working out what to search for...")
            plan = ai.build_search_plan(user_request, province, master)
            say("Searching for: " + ", ".join(plan["queries"][:5]))
        except Exception as e:
            core.log(f"plan failed: {e}")

    if not plan:
        base = [w for w in re.split(r"[,\n]", user_request) if w.strip()]
        if not base:
            base = (master.get("skills") or
                    ["general worker", "admin assistant", "cashier",
                     "warehouse assistant", "driver"])[:6]
        plan = {"queries": [str(b).strip() for b in base][:6], "titles": [],
                "locations": core.PROVINCE_CITY_MAP.get(province, [])[:6],
                "must_have": [], "nice_to_have": [], "exclude": []}

    locations = plan.get("locations") or core.PROVINCE_CITY_MAP.get(province, [])
    where = (locations[0] if locations else "South Africa")

    tasks = []
    for q in plan["queries"][:8]:
        tasks += [("adzuna", search_adzuna, q), ("jooble", search_jooble, q),
                  ("jsearch", search_jsearch, q), ("serpapi", search_serpapi, q)]

    collected = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {}
        for name, fn, q in tasks:
            if stop():
                break
            futs[pool.submit(_safe_call, fn, s, q, where)] = (name, q)
        for fut in as_completed(futs):
            if stop():
                break
            name, q = futs[fut]
            try:
                got = fut.result() or []
            except Exception as e:
                core.log(f"{name}/{q}: {e}"); got = []
            if got:
                say(f"  {name}: {len(got)} result(s) for '{q}'")
            collected += got

    conn = core.get_db()
    feeds = [r["url"] for r in
             conn.execute("SELECT url FROM sources WHERE active=1").fetchall()]
    conn.close()
    for url in feeds:
        if stop():
            break
        collected += search_rss(url)

    if not collected:
        say("Nothing came back. Add an Adzuna or JSearch key in Settings - "
            "without one only your RSS feeds are searched.", level="warn")
        return 0

    jobs = _dedupe(collected)[:max_results]
    say(f"{len(jobs)} unique advert(s) found. Checking them...")

    prov_terms = core.PROVINCE_CITY_MAP.get(province, [])
    kept = []
    for j in jobs:
        blob = f"{j['title']} {j['location']} {j['description']}".lower()
        if prov_terms and not any(t in blob for t in prov_terms):
            continue
        if s.get("baseline_filter_hard") and not core.passes_baseline_filter(blob):
            continue
        j["contact_email"] = j.get("contact_email") or core.extract_email(j["description"])
        j["match_score"] = core.calculate_match_score(blob)
        j["scam_score"] = core.calculate_scam_score(blob, j.get("company"))
        j["location"] = j["location"] or province.title()
        kept.append(j)

    if not kept:
        say("Results found, but none matched your province filter.", level="warn")
        return 0

    if ai.available() and master and not stop():
        say(f"Reading {len(kept)} advert(s) against your CV...")
        try:
            verdicts = ai.rank_jobs(kept, master, plan)
            for j in kept:
                v = verdicts.get(j["uid"])
                if not v:
                    continue
                j["ai_score"] = int(v.get("fit") or 0)
                j["ai_reason"] = str(v.get("reason") or "")[:300]
                j["ai_flags"] = ", ".join(v.get("flags") or [])[:300]
                j["scam_score"] = max(j["scam_score"], int(v.get("scam") or 0))
        except Exception as e:
            core.log(f"ranking failed: {e}")
            say("Could not AI-rank these - showing keyword scores instead.", level="warn")

    for j in kept:
        j.setdefault("ai_score", j["match_score"])
        j.setdefault("ai_reason", "")
        j.setdefault("ai_flags", "")

    added = 0
    now = datetime.datetime.now().isoformat()
    default_mode = s.get("default_cv_mode") or core.CV_MODE_CUSTOM
    with core._db_lock:
        conn = core.get_db(); c = conn.cursor()
        for j in kept:
            c.execute("""INSERT OR IGNORE INTO jobs
                (uid, source_id, title, company, location, url, description,
                 contact_email, match_score, scam_score, created_at, cv_mode,
                 email_subject, email_body, email_edited, ai_score, ai_reason,
                 ai_flags, salary, posted, provider)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'','',0,?,?,?,?,?,?)""",
                (j["uid"], j["provider"], j["title"], j["company"], j["location"],
                 j["url"], j["description"], j.get("contact_email", ""),
                 j["match_score"], j["scam_score"], now, default_mode,
                 j["ai_score"], j["ai_reason"], j["ai_flags"],
                 j.get("salary", ""), j.get("posted", ""), j["provider"]))
            added += c.rowcount
        conn.commit(); conn.close()

    say(f"Done. {added} new job(s) added, sorted best-fit first.", done=True)
    return added


def _safe_call(fn, s, q, where):
    try:
        return fn(s, q, where)
    except urllib.error.HTTPError as e:
        core.log(f"{fn.__name__} HTTP {e.code}")
        return []
    except Exception as e:
        core.log(f"{fn.__name__}: {e}")
        return []
