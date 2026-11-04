#!/usr/bin/env python3
"""
JobBot Core - Android-safe engine.
No CLI, no stdin, no subprocess, no Flask. Every long-running call
accepts a `progress` callback and is designed to run from a worker thread.
"""

import os
import re
import ssl
import json
import time
import random
import shutil
import sqlite3
import hashlib
import smtplib
import datetime
import threading
import traceback
import unicodedata
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from collections import Counter
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication

try:
    from fpdf import FPDF
except ImportError:
    FPDF = None
try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None


# ==========================================================
# WRITABLE STORAGE (Android critical)
# ==========================================================
def _android_files_dir():
    try:
        from android.storage import app_storage_path
        return app_storage_path()
    except Exception:
        pass
    try:
        from jnius import autoclass
        activity = autoclass('org.kivy.android.PythonActivity').mActivity
        return activity.getFilesDir().getAbsolutePath()
    except Exception:
        pass
    try:
        from kivy.app import App
        running = App.get_running_app()
        if running:
            return running.user_data_dir
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), ".jobbot")


BASE_DIR      = _android_files_dir()
CONFIG_DIR    = os.path.join(BASE_DIR, "config")
DATA_DIR      = os.path.join(BASE_DIR, "data")
OUT_DIR       = os.path.join(BASE_DIR, "out")
CVS_DIR       = os.path.join(OUT_DIR, "cvs")
OWN_CV_DIR    = os.path.join(DATA_DIR, "own_cv")
CACHE_DIR     = os.path.join(BASE_DIR, "cache")
LOG_PATH      = os.path.join(BASE_DIR, "jobbot.log")

DB_PATH        = os.path.join(DATA_DIR, "jobbot.db")
MASTER_CV_PATH = os.path.join(DATA_DIR, "master_cv.json")
CONFIG_PATH    = os.path.join(CONFIG_DIR, "settings.json")
BLOCKLIST_PATH = os.path.join(DATA_DIR, "blocklist.txt")

os.environ.setdefault("TMPDIR", CACHE_DIR)

FONT, BODY, LEAD = "Helvetica", 10.5, 5.6

CV_MODE_CUSTOM = "custom"
CV_MODE_OWN = "own"
CV_MODE_LABELS = {CV_MODE_CUSTOM: "custom ats cv",
                  CV_MODE_OWN: "own cv",
                  "": "(not chosen yet)"}
OWN_CV_ALLOWED_SUFFIXES = (".pdf", ".docx", ".txt", ".md")

DEFAULT_SEND_DELAY = 12
DEFAULT_SEND_JITTER = 5
DEFAULT_MAX_PER_RUN = 40
GMAIL_DAILY_SOFT_LIMIT = 500

DEFAULT_SETTINGS = {
    "own_cv_path": "",
    "default_cv_mode": CV_MODE_CUSTOM,
    "province": "",
    "send_delay_seconds": DEFAULT_SEND_DELAY,
    "send_delay_jitter": DEFAULT_SEND_JITTER,
    "max_emails_per_run": DEFAULT_MAX_PER_RUN,
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 465,
    "smtp_user": "",
    "smtp_pass": "",
    "ai_enabled": True,
    "ai_base_url": "https://api.openai.com/v1",
    "ai_api_key": "",
    "ai_model": "gpt-4o-mini",
    "adzuna_app_id": "",
    "adzuna_app_key": "",
    "adzuna_country": "za",
    "jooble_key": "",
    "jooble_domain": "za.jooble.org",
    "rapidapi_key": "",
    "serpapi_key": "",
    "baseline_filter_hard": False,
}


def log(msg):
    try:
        ensure_directories()
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now().isoformat()} {msg}\n")
    except Exception:
        pass


def ensure_directories():
    for d in (CONFIG_DIR, DATA_DIR, OUT_DIR, CVS_DIR, OWN_CV_DIR, CACHE_DIR):
        os.makedirs(d, exist_ok=True)
    if not os.path.exists(BLOCKLIST_PATH):
        Path(BLOCKLIST_PATH).write_text(
            "# one keyword or domain per line\ntelegram\nwhatsapp only\n"
            "pay for registration\nregistration fee\n", encoding="utf-8")
    if not os.path.exists(CONFIG_PATH):
        Path(CONFIG_PATH).write_text(json.dumps(DEFAULT_SETTINGS, indent=2),
                                     encoding="utf-8")


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        if os.path.exists(CONFIG_PATH):
            loaded = json.loads(Path(CONFIG_PATH).read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                s.update(loaded)
    except Exception as e:
        log(f"load_settings failed: {e}")
    return s


def save_settings(s):
    ensure_directories()
    Path(CONFIG_PATH).write_text(json.dumps(s, indent=2, ensure_ascii=False),
                                 encoding="utf-8")


# ==========================================================
# DATABASE
# ==========================================================
_db_lock = threading.Lock()


def get_db():
    ensure_directories()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
    except Exception:
        pass
    return conn


def init_db():
    ensure_directories()
    with _db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS sources (
            id TEXT PRIMARY KEY, url TEXT UNIQUE, name TEXT,
            active INTEGER DEFAULT 1, last_fetched TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS jobs (
            uid TEXT PRIMARY KEY, source_id TEXT, title TEXT, company TEXT,
            location TEXT, url TEXT, description TEXT, contact_email TEXT,
            match_score INTEGER DEFAULT 0, scam_score INTEGER DEFAULT 0,
            selected INTEGER DEFAULT 0, status TEXT DEFAULT 'new',
            created_at TEXT, cv_mode TEXT DEFAULT '',
            email_subject TEXT DEFAULT '', email_body TEXT DEFAULT '',
            email_edited INTEGER DEFAULT 0,
            ai_score INTEGER DEFAULT 0, ai_reason TEXT DEFAULT '',
            ai_flags TEXT DEFAULT '', salary TEXT DEFAULT '',
            posted TEXT DEFAULT '', provider TEXT DEFAULT '')""")
        c.execute("""CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT, job_uid TEXT, company TEXT,
            title TEXT, contact_email TEXT, cv_path TEXT, sent_at TEXT,
            status TEXT, cv_mode TEXT DEFAULT '',
            email_subject TEXT DEFAULT '', email_body TEXT DEFAULT '')""")
        have = {r[1] for r in c.execute("PRAGMA table_info(jobs)").fetchall()}
        for col, ddl in (("ai_score", "INTEGER DEFAULT 0"),
                         ("ai_reason", "TEXT DEFAULT ''"),
                         ("ai_flags", "TEXT DEFAULT ''"),
                         ("salary", "TEXT DEFAULT ''"),
                         ("posted", "TEXT DEFAULT ''"),
                         ("provider", "TEXT DEFAULT ''")):
            if col not in have:
                try:
                    c.execute(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}")
                except Exception:
                    pass
        conn.commit()
        conn.close()


# ==========================================================
# REGEX & CONSTANTS
# ==========================================================
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}")
PHONE_RE = re.compile(r"(?:\+27|0)\s?(?:\d[\s-]?){8,9}\d")
DATE_RE = re.compile(
    r"((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s*)?"
    r"(19|20)\d{2}\s*(?:-|to|until|\u2013)\s*"
    r"(present|current|now|((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
    r"[a-z]*\s*)?(19|20)\d{2})", re.I)
YEARS_RE = re.compile(r"(\d{1,2})\+?\s*(?:years|yrs)\b", re.I)

SECTIONS = {
    "profile": r"(profile|summary|objective|about me|personal statement|career summary|professional summary)",
    "skills": r"(skills|competenc|strengths|proficienc|abilities|attributes)",
    "experience": r"(experience|employment|work history|career history|previous position)",
    "education": r"(education|qualification|academic|schooling)",
    "certs": r"(certificat|courses|training|licence|license|accreditation)",
    "references": r"(reference|referee)",
}

NAME_EXCLUDE_PHRASES = {"curriculum vitae", "resume", "résumé", "cv",
                        "personal details", "contact details",
                        "contact information", "biodata", "profile"}

DUTY_VERB_STARTS = {
    "assisted", "handled", "managed", "responsible", "performed", "provided",
    "maintained", "organized", "organised", "processed", "served", "answered",
    "prepared", "checked", "cleaned", "packed", "loaded", "unloaded",
    "captured", "filed", "restocked", "supervised", "trained", "delivered",
    "monitored", "resolved", "operated", "conducted", "supported", "sold",
}

STOP = set("""
a an the and or but if then than that this these those with without within for
to of in on at by from as is are was were be been being it its their there here
we you your our will shall can may must should would could have has had do does
did not no yes all any each other more most such only own same so too very job
role position candidate company applicant successful required requirements
duties responsibilities please apply application work working experience ability
able strong good excellent must-have year years month months per day week team
environment based salary market related benefits opportunity
""".split())

SA_PHRASES = ["grade 12", "matric", "ncv level 4", "nqf level 4",
    "senior certificate", "code 8", "code 10", "code 14", "drivers licence",
    "driver's licence", "own transport", "forklift", "pdp", "first aid",
    "health and safety", "customer service", "stock control", "cash handling",
    "data capturing", "microsoft office", "ms excel", "ms word", "pastel",
    "sage", "sap", "call centre", "cold calling", "merchandising",
    "quality control", "picking and packing", "general worker", "security",
    "psira", "cleaning", "admin", "reception", "filing", "invoicing",
    "debtors", "creditors", "inventory", "warehouse", "retail", "sales",
    "cashier", "customer care"]

QUALIFICATION_TERMS = ["grade 12", "matric", "ncv level 4", "nqf level 4",
    "senior certificate", "qualification", "certificate", "diploma", "degree"]
LICENCE_TERMS = ["drivers licence", "driver's licence", "code 8", "code 10",
    "code 14", "pdp", "forklift licence", "forklift license", "psira"]
BASELINE_QUALIFICATION_TERMS = ["grade 12", "matric", "senior certificate",
    "ncv level 4", "nqf level 4", "national certificate vocational level 4",
    "national senior certificate"]
BASELINE_LICENCE_TERMS = ["drivers licence", "driver's licence",
    "drivers license", "driver's license", "code 8", "code 10", "code 14",
    "code eb", "code c1", "pdp"]

SKILL_TERMS = ["customer service", "customer care", "communication", "teamwork",
    "cash handling", "cashier", "sales", "stock control", "inventory",
    "data capturing", "microsoft office", "ms word", "ms excel", "excel",
    "filing", "administration", "admin", "reception", "cleaning",
    "merchandising", "quality control", "health and safety", "time management",
    "problem solving", "attention to detail", "picking and packing",
    "warehouse", "retail"]
DUTY_TERMS = ["assist customers", "serve customers", "receive stock",
    "stock control", "pack stock", "pick orders", "pack orders", "load",
    "unload", "clean", "file documents", "capture data", "answer calls",
    "process payments", "handle cash", "maintain records", "merchandise",
    "check stock", "organize stock", "prepare orders"]
PREFERRED_MARKERS = ["preferred", "advantage", "advantageous",
    "added advantage", "would be an advantage", "nice to have", "plus"]
REQUIRED_MARKERS = ["required", "must have", "must-have", "essential",
    "minimum requirement", "requirements", "you need", "applicants must"]
TRANSFER_MAP = {
    "customer service": ["customer", "customers", "assisted customers",
        "served customers", "client", "clients", "sales"],
    "cash handling": ["cash", "payments", "sales", "tuck shop", "cashier"],
    "stock control": ["stock", "supplies", "products", "merchandise",
        "inventory", "organized supplies", "maintained supplies"],
    "inventory": ["stock", "supplies", "products", "merchandise", "inventory"],
    "teamwork": ["team", "worked with colleagues", "assisted staff", "coworkers"],
    "communication": ["customers", "clients", "communication", "assisted",
        "explained", "answered questions"],
    "sales": ["sales", "sold", "cash", "purchases", "customers"],
    "cleaning": ["clean", "cleanliness", "maintained work area", "sanitation"],
    "administration": ["records", "filing", "documents", "data", "office", "admin"],
    "data capturing": ["records", "data", "documents", "computer", "excel"],
    "warehouse": ["stock", "inventory", "supplies", "packing", "orders"],
}
NEGATION_TERMS = {"no", "not", "without", "n/a", "none", "never", "excluding",
    "lacking", "lack", "isn't", "wasn't", "don't", "doesn't", "didn't"}
NEGATION_WINDOW = 4

PROVINCE_CITY_MAP = {
    "eastern cape": ["eastern cape", "gqeberha", "port elizabeth", "east london",
        "mthatha", "uitenhage", "kariega", "king williams town", "qonce",
        "grahamstown", "makhanda", "queenstown", "komani", "butterworth",
        "graaff-reinet", "jeffreys bay", "humansdorp", "cradock", "aliwal north",
        "stutterheim"],
    "free state": ["free state", "bloemfontein", "mangaung", "welkom",
        "bethlehem", "sasolburg", "kroonstad", "parys", "phuthaditjhaba",
        "harrismith", "virginia", "odendaalsrus", "botshabelo", "bothaville",
        "ficksburg"],
    "gauteng": ["gauteng", "johannesburg", "joburg", "pretoria", "tshwane",
        "soweto", "sandton", "midrand", "centurion", "kempton park", "benoni",
        "boksburg", "germiston", "roodepoort", "randburg", "vanderbijlpark",
        "vereeniging", "springs", "alberton", "krugersdorp", "brakpan",
        "edenvale", "randfontein", "carletonville", "tembisa"],
    "kwazulu-natal": ["kwazulu-natal", "kwazulu natal", "kzn", "durban",
        "ethekwini", "pietermaritzburg", "richards bay", "newcastle",
        "pinetown", "umlazi", "ladysmith", "empangeni", "howick", "kokstad",
        "ballito", "margate", "port shepstone", "estcourt", "vryheid"],
    "limpopo": ["limpopo", "polokwane", "tzaneen", "mokopane", "thohoyandou",
        "musina", "phalaborwa", "lephalale", "giyani", "bela-bela",
        "modimolle", "louis trichardt", "makhado"],
    "mpumalanga": ["mpumalanga", "nelspruit", "mbombela", "witbank",
        "emalahleni", "secunda", "standerton", "ermelo", "middelburg",
        "barberton", "sabie", "white river", "hazyview", "piet retief"],
    "north west": ["north west", "rustenburg", "klerksdorp", "mahikeng",
        "mafikeng", "potchefstroom", "brits", "vryburg", "lichtenburg",
        "zeerust", "christiana", "stilfontein", "orkney", "wolmaransstad",
        "koster", "ventersdorp", "taung"],
    "northern cape": ["northern cape", "kimberley", "upington", "springbok",
        "kuruman", "de aar", "kathu", "colesberg", "calvinia", "postmasburg",
        "kakamas", "warrenton"],
    "western cape": ["western cape", "cape town", "stellenbosch", "paarl",
        "george", "worcester", "mossel bay", "knysna", "hermanus", "oudtshoorn",
        "bellville", "parow", "somerset west", "malmesbury", "vredenburg",
        "saldanha", "swellendam", "beaufort west"],
}


def passes_baseline_filter(text):
    low = (text or "").lower()
    return any(t in low for t in BASELINE_QUALIFICATION_TERMS) or \
           any(t in low for t in BASELINE_LICENCE_TERMS)


# ==========================================================
# CV PARSING
# ==========================================================
def _from_pdf(path):
    if not PdfReader:
        raise ImportError("pypdf is required to read PDF files.")
    reader = PdfReader(str(path))
    return "\n".join((p.extract_text() or "") for p in reader.pages)


def _from_docx(path):
    with zipfile.ZipFile(path) as z:
        w_p = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"
        w_t = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
        root = ET.fromstring(z.read("word/document.xml"))
        lines = []
        for para in root.iter(w_p):
            lines.append("".join(t.text or "" for t in para.iter(w_t)))
    return "\n".join(lines)


def _clean(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\u2018\u2019]", "'", text)
    text = re.sub(r"[\u201c\u201d]", '"', text)
    text = re.sub(r"[\u2013\u2014]", "-", text)
    text = re.sub(r"[\u2022\u25cf\u25aa\u00b7]", "- ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(ln.strip() for ln in text.split("\n")).strip()


def extract_text(path):
    path = Path(str(path))
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        raw = _from_pdf(path)
    elif suffix == ".docx":
        raw = _from_docx(path)
    elif suffix in (".txt", ".md"):
        raw = path.read_text(encoding="utf-8", errors="ignore")
    else:
        raise ValueError(f"'{suffix}' is not supported on mobile. "
                         f"Use one of: .pdf, .docx, .txt, .md")
    out = _clean(raw)
    if len(out) < 80:
        raise ValueError("Almost no text found. The document may be scanned.")
    return out


def _is_heading(line):
    stripped = line.strip().rstrip(":").strip()
    if not stripped or len(stripped) > 55 or stripped.endswith("."):
        return None
    low = stripped.lower()
    for key, pattern in SECTIONS.items():
        if re.fullmatch(rf"[^a-z]*{pattern}[a-z ]*", low):
            return key
    return None


def _split_sections(text):
    out = {"_header": []}
    current = "_header"
    for line in text.split("\n"):
        heading = _is_heading(line)
        if heading:
            current = heading
            out.setdefault(current, [])
        else:
            out.setdefault(current, []).append(line)
    return {k: [ln for ln in v if ln.strip()] for k, v in out.items()}


def _contact(header, full):
    em = EMAIL_RE.search(full)
    ph = PHONE_RE.search(full)
    name = ""
    for line in header[:8]:
        cand = line.strip()
        words = cand.split()
        if cand.lower() in NAME_EXCLUDE_PHRASES:
            continue
        if 1 < len(words) <= 4 and not any(c.isdigit() for c in cand) and "@" not in cand:
            name = cand.title()
            break
    location = ""
    for line in header[:12]:
        if re.search(r"(gauteng|north west|limpopo|mpumalanga|free state|kwazulu|"
                     r"eastern cape|western cape|northern cape|johannesburg|pretoria|"
                     r"rustenburg|mahikeng|klerksdorp|potchefstroom|durban|cape town|parow)",
                     line, re.I):
            location = line.strip(" ,|-")
            break
    return {"name": name or "YOUR NAME",
            "email": em.group(0) if em else "",
            "phone": ph.group(0).strip() if ph else "",
            "location": location}


def _bulletise(lines):
    items = []
    for line in lines:
        line = re.sub(r"^[-*\u2022\d.)\s]+", "", line).strip()
        if line:
            items.append(line)
    return items


def _looks_like_role_header(line):
    stripped = line.strip()
    if not stripped or stripped.startswith(("-", "*", "\u2022")):
        return False
    if DATE_RE.search(stripped):
        return True
    if len(stripped) >= 90 or stripped.endswith("."):
        return False
    if re.search(r"\bat\s+[A-Z]", stripped):
        return True
    if "," in stripped and len(stripped.split()) <= 8:
        first = stripped.split()[0].strip(".,").lower()
        if first not in DUTY_VERB_STARTS:
            return True
    return False


def _experience(lines):
    roles, current = [], None
    for line in lines:
        if _looks_like_role_header(line):
            stripped = line.strip()
            if current and not current["bullets"] and DATE_RE.search(stripped) \
                    and not DATE_RE.search(current["header"]):
                current["header"] = f"{current['header']} ({stripped})"
                continue
            if current:
                roles.append(current)
            current = {"header": stripped, "bullets": [], "skills_demonstrated": []}
        elif current:
            cleaned = re.sub(r"^[-*\u2022\s]+", "", line).strip()
            if cleaned:
                current["bullets"].append(cleaned)
    if current:
        roles.append(current)
    return roles


def _infer_demonstrated_skills(bullets):
    text = " ".join(bullets).lower()
    found = []
    for skill, evidence in TRANSFER_MAP.items():
        for term in evidence:
            if term in text and not _is_negated(text, term):
                found.append(skill)
                break
    return sorted(set(found))


def _infer_years_experience(text):
    matches = [int(m.group(1)) for m in YEARS_RE.finditer(text)]
    return max(matches) if matches else None


def _dedupe_preserve_case(items):
    seen, out = set(), []
    for item in items:
        if not item:
            continue
        c = str(item).strip()
        if c and c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)
    return out


def parse_cv(text):
    sec = _split_sections(text)
    skills_raw = " , ".join(sec.get("skills", []))
    skills = _dedupe_preserve_case(
        [s.strip(" .;") for s in re.split(r"[,;|]|\s{2,}", skills_raw)
         if 2 < len(s.strip()) < 60])[:40]
    experience = _experience(sec.get("experience", []))
    for role in experience:
        role["skills_demonstrated"] = _infer_demonstrated_skills(role.get("bullets", []))
    return {
        "contact": _contact(sec.get("_header", []), text),
        "profile": " ".join(sec.get("profile", []))[:1000],
        "skills": skills,
        "experience": experience,
        "education": _bulletise(sec.get("education", [])),
        "certifications": _bulletise(sec.get("certs", [])),
        "references": _bulletise(sec.get("references", [])) or ["Available on request"],
        "years_experience": _infer_years_experience(text),
    }


def normalize_master_cv(data):
    if "contact" not in data or not isinstance(data.get("contact"), dict):
        data["contact"] = {"name": data.get("full_name") or data.get("name") or "YOUR NAME",
                           "email": data.get("email") or "",
                           "phone": data.get("phone") or "",
                           "location": data.get("location") or ""}
    if not data.get("profile") and data.get("summary"):
        data["profile"] = data["summary"]
    if data.get("certifications") is None:
        data["certifications"] = data.get("certs") or []
    if not data.get("references"):
        data["references"] = ["Available on request"]
    if data.get("experience"):
        norm = []
        for exp in data["experience"]:
            if isinstance(exp, dict):
                header = exp.get("header") or f"{exp.get('role','')} | {exp.get('company','')}".strip(" |")
                bullets = exp.get("bullets") or exp.get("duties") or []
                if isinstance(bullets, str):
                    bullets = [bullets]
                norm.append({"header": header, "bullets": bullets,
                             "skills_demonstrated": exp.get("skills_demonstrated")
                             or _infer_demonstrated_skills(bullets)})
        data["experience"] = norm
    return data


# ==========================================================
# JOB ANALYSIS
# ==========================================================
def _tokens(text):
    return [w for w in re.findall(r"[a-z][a-z+#./'-]{2,}", text.lower()) if w not in STOP]


def _extract_known_terms(job_text, term_list):
    low = job_text.lower()
    return [t for t in term_list if t in low]


def job_keywords(job_text, top=35):
    low = job_text.lower()
    found = [p for p in SA_PHRASES if p in low]
    found_words = {w for phrase in found for w in phrase.split()}
    counts = Counter(_tokens(job_text))
    singles = [w for w, _ in counts.most_common(80) if w not in found_words]
    result = []
    for item in found + singles:
        if item not in result:
            result.append(item)
    return result[:top]


def analyze_job(job_text):
    low = job_text.lower()
    unique = lambda x: list(dict.fromkeys(x))
    return {
        "qualifications": unique(_extract_known_terms(low, QUALIFICATION_TERMS)),
        "licences": unique(_extract_known_terms(low, LICENCE_TERMS)),
        "skills": unique(_extract_known_terms(low, SKILL_TERMS)),
        "duties": unique(_extract_known_terms(low, DUTY_TERMS)),
        "preferred_markers_present": any(m in low for m in PREFERRED_MARKERS),
        "required_markers_present": any(m in low for m in REQUIRED_MARKERS),
        "keywords": job_keywords(job_text),
    }


def _cv_blob(master):
    parts = [master.get("profile") or "",
             " ".join(master.get("skills") or []),
             " ".join(master.get("education") or []),
             " ".join(master.get("certifications") or [])]
    for role in (master.get("experience") or []):
        if isinstance(role, dict):
            parts.append(role.get("header") or "")
            parts.extend(role.get("bullets") or [])
            parts.extend(role.get("skills_demonstrated") or [])
    return " ".join(parts).lower()


def _is_negated(text, term):
    low = text.lower()
    lt = term.lower()
    occ = [m.start() for m in re.finditer(re.escape(lt), low)]
    if not occ:
        return False
    for idx in occ:
        prefix = low[max(0, idx - 40):idx]
        if not any(w in NEGATION_TERMS for w in
                   re.findall(r"[a-z']+", prefix)[-NEGATION_WINDOW:]):
            return False
    return True


def _direct_match(term, master):
    blob = _cv_blob(master)
    return term.lower() in blob and not _is_negated(blob, term)


def _transfer_match(term, master):
    target = term.lower()
    evidence = []
    aliases = TRANSFER_MAP.get(target, [])
    for role in (master.get("experience") or []):
        if isinstance(role, dict):
            rt = " ".join([role.get("header") or ""] + (role.get("bullets") or [])
                          + (role.get("skills_demonstrated") or [])).lower()
            for alias in aliases:
                if alias.lower() in rt and not _is_negated(rt, alias):
                    evidence.append(role.get("header") or "Experience")
                    break
    return evidence


def classify_requirement(term, master):
    if _direct_match(term, master):
        return {"term": term, "status": "direct",
                "evidence": ["Exact/explicit evidence in master CV."]}
    ev = _transfer_match(term, master)
    if ev:
        return {"term": term, "status": "transferable", "evidence": ev}
    return {"term": term, "status": "missing", "evidence": []}


def build_match_report(master, analysis):
    all_terms = list(dict.fromkeys(
        analysis["qualifications"] + analysis["licences"] + analysis["skills"]
        + analysis["duties"] + analysis["keywords"]))
    reqs = [classify_requirement(t, master) for t in all_terms]
    direct = [x["term"] for x in reqs if x["status"] == "direct"]
    transferable = [x["term"] for x in reqs if x["status"] == "transferable"]
    missing = [x["term"] for x in reqs if x["status"] == "missing"]
    cov = round(100 * (len(direct) + len(transferable)) / max(len(reqs), 1))
    return {"requirements": reqs, "direct": direct,
            "transferable": transferable, "missing": missing,
            "keyword_coverage_percent": cov}


# ==========================================================
# TAILORING & PDF
# ==========================================================
def _score(text, keywords):
    low = text.lower()
    score = 0
    for k in keywords:
        if k.lower() in low:
            score += 2 if " " in k else 1
    return score


def _relevant_skills(master, analysis):
    keywords = analysis.get("keywords") or []
    skills = master.get("skills") or []
    ranked = sorted(skills, key=lambda s: _score(str(s), keywords), reverse=True)
    return _dedupe_preserve_case(ranked)[:16]


def _relevant_bullets(role, analysis):
    keywords = analysis.get("keywords") or []
    bullets = role.get("bullets") or []
    sd = role.get("skills_demonstrated") or []
    ranked = sorted(bullets, key=lambda b: _score(str(b) + " " + " ".join(sd), keywords), reverse=True)
    return ranked[:6]


def _summary(master, job, match):
    direct = (match.get("direct") or [])[:4]
    transfer = (match.get("transferable") or [])[:3]
    pieces = []
    if master.get("profile"):
        pieces.append(str(master["profile"]).strip())
    if direct:
        pieces.append("Relevant strengths include " + ", ".join(direct) + ".")
    if transfer:
        pieces.append("Transferable strengths include " + ", ".join(transfer) + ".")
    if not pieces:
        pieces.append(f"Motivated candidate seeking to contribute to the {job.get('title','position')} role.")
    return re.sub(r"\s+", " ", " ".join(pieces)).strip()[:900]


def tailor(master, job):
    master = normalize_master_cv(master)
    analysis = analyze_job(f"{job.get('title','')} {job.get('company') or ''} {job.get('description') or ''}")
    match = build_match_report(master, analysis)
    roles = []
    for role in (master.get("experience") or []):
        if isinstance(role, dict):
            b = _relevant_bullets(role, analysis)
            roles.append({"header": role.get("header") or "", "bullets": b,
                          "relevance": _score((role.get("header") or "") + " " + " ".join(b),
                                              analysis["keywords"])})
    roles.sort(key=lambda r: r["relevance"], reverse=True)
    return {"contact": master.get("contact") or {},
            "target_title": job.get("title", ""),
            "target_company": job.get("company") or "",
            "profile": _summary(master, job, match),
            "skills": _relevant_skills(master, analysis),
            "experience": roles,
            "education": master.get("education") or [],
            "certifications": master.get("certifications") or [],
            "references": master.get("references") or ["Available on request"],
            "_analysis": analysis, "_match": match}


def _safe(text):
    text = str(text)
    text = (text.replace("\u2022", "-").replace("\u2013", "-").replace("\u2014", "-")
            .replace("\u2018", "'").replace("\u2019", "'").replace("\u201c", '"')
            .replace("\u201d", '"').replace("\u00a0", " "))
    out = []
    for ch in text:
        try:
            ch.encode("latin-1")
            out.append(ch)
        except UnicodeEncodeError:
            d = unicodedata.normalize("NFKD", ch)
            out.append(d.encode("ascii", "ignore").decode("ascii"))
    return re.sub(r"\s+", " ", "".join(out)).strip()


if FPDF:
    class ATSCv(FPDF):
        def header(self): pass
        def footer(self): pass


def _section(pdf, title):
    pdf.ln(2.5)
    pdf.set_font(FONT, "B", 11)
    pdf.cell(0, 6, _safe(title.upper()), new_x="LMARGIN", new_y="NEXT")
    pdf.set_draw_color(120, 120, 120)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(1.5)
    pdf.set_font(FONT, "", BODY)


def render_pdf(cv, out_path):
    if not FPDF:
        return None
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pdf = ATSCv(format="A4", unit="mm")
    pdf.set_auto_page_break(True, margin=14)
    pdf.set_margins(16, 14, 16)
    c = cv.get("contact") or {}
    name = c.get("name") or "Applicant"
    pdf.set_title(f"{name} - CV - {cv.get('target_title','')}")
    pdf.set_author(name)
    pdf.set_creator("JobBot CV Engine")
    pdf.add_page()
    pdf.set_font(FONT, "B", 17)
    pdf.cell(0, 9, _safe(name), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(FONT, "", 9.5)
    cl = " | ".join(x for x in (c.get("phone"), c.get("email"), c.get("location")) if x)
    if cl:
        pdf.multi_cell(0, 5, _safe(cl))
    if cv.get("target_title"):
        pdf.ln(1)
        pdf.set_font(FONT, "B", 11)
        pdf.multi_cell(0, 5.5, _safe(f"Application for: {cv['target_title']}"))
    if cv.get("profile"):
        _section(pdf, "Professional Summary")
        pdf.multi_cell(0, LEAD, _safe(cv["profile"]))
    if cv.get("skills"):
        _section(pdf, "Key Skills")
        for s in cv["skills"]:
            pdf.multi_cell(0, LEAD, _safe(f"- {s}"))
    if cv.get("experience"):
        _section(pdf, "Work Experience")
        for r in cv["experience"]:
            if r.get("header"):
                pdf.set_font(FONT, "B", BODY)
                pdf.multi_cell(0, LEAD, _safe(r["header"]))
            pdf.set_font(FONT, "", BODY)
            for b in r.get("bullets") or []:
                pdf.multi_cell(0, LEAD, _safe(f"- {b}"))
            pdf.ln(1.2)
    for title, key in (("Education", "education"),
                       ("Certifications and Licences", "certifications"),
                       ("References", "references")):
        if cv.get(key):
            _section(pdf, title)
            for item in cv[key]:
                pdf.multi_cell(0, LEAD, _safe(f"- {item}"))
    pdf.output(str(out_path))
    return out_path


def ats_preview(path):
    if not PdfReader:
        return "(Install pypdf to preview)"
    text = "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)
    return text.strip() or "(NO TEXT FOUND)"


def validate_ats(path, cv=None):
    if not PdfReader:
        return {"status": "UNCHECKED", "passed": 0, "total": 0,
                "reason": "pypdf not installed"}
    if Path(path).suffix.lower() != ".pdf":
        return {"status": "UNCHECKED", "passed": 0, "total": 0,
                "reason": "not a PDF"}
    reader = PdfReader(str(path))
    raw = "\n".join((p.extract_text() or "") for p in reader.pages).strip()
    low = raw.lower()
    name = cv.get("contact", {}).get("name", "") if cv and isinstance(cv.get("contact"), dict) else ""
    checks = {
        "text_extractable": bool(raw),
        "name_present": bool(name and name.lower() in low) if name else bool(raw),
        "email_present": bool(EMAIL_RE.search(raw)),
        "phone_present": bool(PHONE_RE.search(raw)),
        "summary_heading": "professional summary" in low,
        "skills_heading": "key skills" in low,
        "experience_heading": "work experience" in low,
        "education_heading": "education" in low,
        "reasonable_page_count": 1 <= len(reader.pages) <= 3,
    }
    passed = sum(checks.values())
    total = len(checks)
    return {"checks": checks, "passed": passed, "total": total,
            "status": "PASS" if passed == total else "REVIEW",
            "page_count": len(reader.pages),
            "extractable_characters": len(raw)}


# ==========================================================
# SCORING
# ==========================================================
def calculate_scam_score(text, company=""):
    score = 0
    t = (text or "").lower()
    if os.path.exists(BLOCKLIST_PATH):
        with open(BLOCKLIST_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip().lower()
                if line and not line.startswith("#") and line in t:
                    score += 40
    for kw in ["telegram", "whatsapp only", "registration fee", "wire transfer",
               "crypto", "no experience $500/day"]:
        if kw in t:
            score += 25
    if not company or company.lower() in ["unknown", "n/a", ""]:
        score += 15
    return min(score, 100)


def calculate_match_score(text, keywords=None):
    if not keywords:
        keywords = ["customer service", "admin", "office", "tourism",
                    "assistant", "receptionist", "clerk"]
    score = 30
    t = (text or "").lower()
    for kw in keywords:
        if kw.lower() in t:
            score += 10
    return min(score, 100)


def extract_email(text):
    m = EMAIL_RE.search(text or "")
    return m.group(0) if m else ""


# ==========================================================
# OWN CV / MASTER CV MANAGEMENT
# ==========================================================
def get_own_cv_path():
    s = load_settings()
    cfg = (s.get("own_cv_path") or "").strip()
    if cfg:
        p = Path(cfg)
        if p.exists() and p.is_file():
            return p
    if os.path.isdir(OWN_CV_DIR):
        cands = [Path(OWN_CV_DIR) / f for f in os.listdir(OWN_CV_DIR)
                 if Path(f).suffix.lower() in OWN_CV_ALLOWED_SUFFIXES]
        if cands:
            return sorted(cands, key=lambda p: p.stat().st_mtime, reverse=True)[0]
    return None


def store_own_cv(src):
    ensure_directories()
    src = Path(str(src))
    if not src.exists():
        raise FileNotFoundError(f"File not found: {src}")
    if src.suffix.lower() not in OWN_CV_ALLOWED_SUFFIXES:
        raise ValueError(f"'{src.suffix}' is not supported on mobile. "
                         f"Use one of: {', '.join(OWN_CV_ALLOWED_SUFFIXES)}")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", src.name)
    dest = Path(OWN_CV_DIR) / safe
    if src.resolve() != dest.resolve():
        shutil.copy2(src, dest)
    s = load_settings()
    s["own_cv_path"] = str(dest)
    save_settings(s)
    return dest


def import_master_cv(path):
    text = extract_text(path)
    data = parse_cv(text)
    ensure_directories()
    Path(MASTER_CV_PATH).write_text(json.dumps(data, indent=2, ensure_ascii=False),
                                    encoding="utf-8")
    return data


def load_master_cv():
    if not os.path.exists(MASTER_CV_PATH):
        return None
    try:
        return json.loads(Path(MASTER_CV_PATH).read_text(encoding="utf-8"))
    except Exception as e:
        log(f"master cv unreadable: {e}")
        return None


def set_job_cv_mode(uid, mode, select=True):
    with _db_lock:
        conn = get_db(); c = conn.cursor()
        if select:
            c.execute("UPDATE jobs SET cv_mode=?, selected=1 WHERE uid=?", (mode, uid))
        else:
            c.execute("UPDATE jobs SET cv_mode=? WHERE uid=?", (mode, uid))
        changed = c.rowcount
        conn.commit(); conn.close()
    return bool(changed)


def toggle_job(uid):
    with _db_lock:
        conn = get_db(); c = conn.cursor()
        c.execute("UPDATE jobs SET selected = CASE WHEN selected=1 THEN 0 ELSE 1 END WHERE uid=?", (uid,))
        conn.commit()
        row = c.execute("SELECT selected FROM jobs WHERE uid=?", (uid,)).fetchone()
        conn.close()
    return bool(row and row["selected"])


def list_jobs(selected_only=False):
    conn = get_db(); c = conn.cursor()
    q = "SELECT * FROM jobs {} ORDER BY ai_score DESC, match_score DESC"
    c.execute(q.format("WHERE selected=1" if selected_only else ""))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def save_job_email(uid, subject, body, mark_edited=True):
    with _db_lock:
        conn = get_db(); c = conn.cursor()
        c.execute("UPDATE jobs SET email_subject=?, email_body=?, email_edited=? WHERE uid=?",
                  (subject, body, 1 if mark_edited else 0, uid))
        ok = c.rowcount
        conn.commit(); conn.close()
    return bool(ok)


# ==========================================================
# EMAIL LETTER GENERATION
# ==========================================================
EMAIL_GREETINGS_NAMED = [
    "Dear Hiring Team at {company},", "Good day {company} Recruitment Team,",
    "Dear {company} Hiring Manager,", "Good day {company} Team,",
    "Dear Recruitment Team at {company},"]
EMAIL_GREETINGS_GENERIC = ["Good day,", "Dear Hiring Manager,",
    "Dear Recruitment Team,", "Good day Hiring Team,"]
EMAIL_OPENERS = [
    "I would like to apply for the {title} position{where}.",
    "Please accept this email as my application for the {title} vacancy{where}.",
    "I am writing to apply for the advertised {title} role{where}.",
    "I came across your {title} vacancy{where} and would like to submit my application.",
    "I am very interested in the {title} opportunity{where} and would like to be considered."]
EMAIL_CLOSERS = [
    "I would welcome the opportunity to discuss how I can contribute to your team, and I am available for an interview at your convenience.",
    "I would appreciate the chance to discuss my application further and can attend an interview whenever it suits you.",
    "Thank you for taking the time to consider my application. I am ready to start as soon as required.",
    "I would be grateful for the opportunity of an interview to show how my background fits this role.",
    "Thank you for reviewing my application. I am available for an interview and can provide references on request."]
EMAIL_SUBJECTS = [
    "Application for {title} - {name}", "{title} Vacancy - Application from {name}",
    "Application: {title}{loc_suffix} - {name}", "CV for the {title} Position - {name}",
    "{name} - Application for {title}{loc_suffix}"]
EMAIL_SIGNOFFS = ["Kind regards", "Sincerely", "Best regards",
                  "Yours faithfully", "Warm regards"]


def _variant_index(seed, options, salt=""):
    d = hashlib.md5((str(seed) + "|" + salt).encode("utf-8")).hexdigest()
    return int(d, 16) % max(options, 1)


def _company_is_known(company):
    return bool(company) and company.strip().lower() not in {
        "unknown", "n/a", "none", "", "company", "extracted company", "direct import"}


def _pretty_terms(terms, limit=4):
    cleaned = []
    for t in terms[:limit]:
        t = str(t).strip()
        if not t:
            continue
        cleaned.append(t if len(t) > 3 else t.upper())
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]
    return ", ".join(cleaned[:-1]) + " and " + cleaned[-1]


def generate_email_for_job(job, master_cv, cv_mode=CV_MODE_CUSTOM):
    master = normalize_master_cv(dict(master_cv or {}))
    contact = master.get("contact") or {}
    name = contact.get("name") or "Applicant"
    phone = contact.get("phone") or ""
    email = contact.get("email") or ""
    home_loc = contact.get("location") or ""

    uid = job.get("uid") or hashlib.md5(json.dumps(job, default=str, sort_keys=True).encode()).hexdigest()[:12]
    title = (job.get("title") or "the advertised position").strip()
    company = (job.get("company") or "").strip()
    location = (job.get("location") or "").strip()
    url = (job.get("url") or "").strip()
    description = job.get("description") or ""

    analysis = analyze_job(f"{title} {company} {description}")
    match = build_match_report(master, analysis)
    direct = match.get("direct") or []
    transferable = match.get("transferable") or []

    baseline_quals = [t for t in direct if t in BASELINE_QUALIFICATION_TERMS or t in QUALIFICATION_TERMS]
    baseline_licences = [t for t in direct if t in BASELINE_LICENCE_TERMS or t in LICENCE_TERMS]
    direct_skills = [t for t in direct if t in SKILL_TERMS or t in DUTY_TERMS]
    transfer_skills = [t for t in transferable if t in SKILL_TERMS or t in DUTY_TERMS] or transferable

    known_company = _company_is_known(company)
    where = f" in {location}" if location else ""

    subj_template = EMAIL_SUBJECTS[_variant_index(uid, len(EMAIL_SUBJECTS), "subject")]
    loc_suffix = f" ({location})" if location else ""
    subject = subj_template.format(title=title, name=name, loc_suffix=loc_suffix)
    subject = re.sub(r"\s+", " ", subject).strip()[:180]

    if known_company:
        greeting = EMAIL_GREETINGS_NAMED[_variant_index(uid, len(EMAIL_GREETINGS_NAMED), "greet")].format(company=company)
    else:
        greeting = EMAIL_GREETINGS_GENERIC[_variant_index(uid, len(EMAIL_GREETINGS_GENERIC), "greet")]

    paragraphs = []
    opener = EMAIL_OPENERS[_variant_index(uid, len(EMAIL_OPENERS), "open")].format(title=title, where=where)
    if known_company:
        opener += f" I believe my background is a good fit for what {company} is looking for."
    paragraphs.append(opener)

    req_bits = []
    if baseline_quals:
        req_bits.append(f"I meet the stated minimum qualification requirement ({_pretty_terms(baseline_quals, 3)})")
    if baseline_licences:
        req_bits.append(f"I hold the required {_pretty_terms(baseline_licences, 3)}")
    if direct_skills:
        req_bits.append(f"I have hands-on experience in {_pretty_terms(direct_skills, 4)}")
    if req_bits:
        paragraphs.append("Regarding the requirements listed in the advert: " + "; ".join(req_bits) + ".")

    if transfer_skills:
        paragraphs.append("In addition, my previous work has given me closely related experience that transfers directly to "
                          f"{_pretty_terms(transfer_skills, 3)}, and I am a quick learner in areas that are new to me.")

    profile = (master.get("profile") or "").strip()
    if profile:
        trimmed = re.sub(r"\s+", " ", profile)[:320].strip()
        if trimmed and not trimmed.endswith((".", "!", "?")):
            trimmed += "."
        paragraphs.append(trimmed)

    if location and home_loc:
        paragraphs.append(f"I am currently based in {home_loc} and am available to work in {location}.")
    elif location:
        paragraphs.append(f"I am available to work in {location}.")

    if cv_mode == CV_MODE_OWN:
        paragraphs.append("My CV is attached to this email for your consideration.")
    else:
        paragraphs.append(f"Attached is my CV, which I have set out specifically for the {title} role so that the relevant qualifications, skills and duties are easy to find.")

    if url:
        paragraphs.append(f"Advert reference: {url}")
    paragraphs.append(EMAIL_CLOSERS[_variant_index(uid, len(EMAIL_CLOSERS), "close")])

    signoff = EMAIL_SIGNOFFS[_variant_index(uid, len(EMAIL_SIGNOFFS), "sign")]
    sig_lines = [signoff + ",", name]
    if phone:
        sig_lines.append(phone)
    if email:
        sig_lines.append(email)
    if home_loc:
        sig_lines.append(home_loc)

    body = greeting + "\n\n" + "\n\n".join(paragraphs) + "\n\n" + "\n".join(sig_lines) + "\n"
    return subject, body


def ensure_job_email(uid, overwrite=False, ai=None):
    conn = get_db(); c = conn.cursor()
    job = c.execute("SELECT * FROM jobs WHERE uid=?", (uid,)).fetchone()
    conn.close()
    if not job:
        return None
    job = dict(job)
    if (job.get("email_body") or "").strip() and not overwrite:
        return job["email_subject"], job["email_body"]

    master = load_master_cv()
    if not master:
        return None
    mode = job.get("cv_mode") or CV_MODE_CUSTOM

    subject = body = None
    if ai is not None and ai.available():
        try:
            subject, body = ai.write_email(job, master, mode)
        except Exception as e:
            log(f"AI email failed for {uid}: {e}")
    if not body:
        subject, body = generate_email_for_job(job, master, mode)

    save_job_email(uid, subject, body, mark_edited=False)
    return subject, body


# ==========================================================
# ATTACHMENT BUILDER
# ==========================================================
def build_attachment(job, master_cv, mode):
    company = job.get("company") or "Company"
    if mode == CV_MODE_OWN:
        own = get_own_cv_path()
        if not own:
            return None, None, ("This job is set to 'own cv' but no CV file has been uploaded. "
                                "Upload one, or switch it to 'custom ats cv'.")
        return own, {"cv_mode": CV_MODE_LABELS[CV_MODE_OWN], "attachment": str(own)}, None

    if not FPDF:
        return None, None, "fpdf2 is missing from the build - cannot create a PDF."

    tailored = tailor(master_cv, {"title": job.get("title") or "",
                                  "company": company,
                                  "location": job.get("location") or "",
                                  "description": job.get("description") or ""})
    analysis = tailored.pop("_analysis", {})
    match = tailored.pop("_match", {})

    fname = f"CV_{job['uid']}_{re.sub(r'[^A-Za-z0-9]', '_', company)[:30]}.pdf"
    pdf_path = Path(CVS_DIR) / fname
    try:
        render_pdf(tailored, pdf_path)
    except Exception as e:
        log("render_pdf: " + traceback.format_exc())
        return None, None, f"PDF generation failed: {e}"

    report = {"cv_mode": CV_MODE_LABELS[CV_MODE_CUSTOM],
              "keyword_coverage_percent": match.get("keyword_coverage_percent", 0),
              "direct_matches": match.get("direct", []),
              "transferable_matches": match.get("transferable", []),
              "missing": match.get("missing", []),
              "ats_validation": validate_ats(pdf_path, tailored)}
    try:
        pdf_path.with_suffix(".json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return pdf_path, report, None


# ==========================================================
# SEND CONTROLLER
# ==========================================================
class SendController:
    def __init__(self, progress=None, ai=None):
        self.progress = progress or (lambda msg, **kw: None)
        self.ai = ai
        self._cancel = threading.Event()
        self.thread = None
        self.sent = 0

    def cancel(self):
        self._cancel.set()

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self.thread

    def _run(self):
        try:
            self._send_all()
        except Exception as e:
            log("send crash: " + traceback.format_exc())
            self.progress(f"Unexpected error: {e}", level="error")

    def _send_all(self):
        s = load_settings()
        delay = max(0, int(s.get("send_delay_seconds", DEFAULT_SEND_DELAY)))
        jitter = max(0, int(s.get("send_delay_jitter", DEFAULT_SEND_JITTER)))
        cap = max(1, int(s.get("max_emails_per_run", DEFAULT_MAX_PER_RUN)))

        master = load_master_cv()
        if not master:
            self.progress("Import your CV first.", level="error"); return

        host = s.get("smtp_server") or "smtp.gmail.com"
        port = int(s.get("smtp_port") or 465)
        user = (s.get("smtp_user") or (master.get("contact") or {}).get("email") or "").strip()
        pw = (s.get("smtp_pass") or "").strip()

        jobs = list_jobs(selected_only=True)
        if not jobs:
            self.progress("No jobs selected.", level="warn"); return
        if not pw:
            self.progress("No SMTP app password saved - CVs and letters will be prepared but nothing will be sent.", level="warn")

        self.progress(f"Processing {len(jobs)} application(s). Pause: {delay}s +0-{jitter}s.")

        for i, job in enumerate(jobs, 1):
            if self._cancel.is_set():
                self.progress("Cancelled."); break
            if self.sent >= cap:
                self.progress(f"Per-run cap of {cap} reached.", level="warn"); break

            mode = job.get("cv_mode") or s.get("default_cv_mode") or CV_MODE_CUSTOM
            if not job.get("cv_mode"):
                set_job_cv_mode(job["uid"], mode, select=True)

            self.progress(f"[{i}/{len(jobs)}] {job['title']} - {CV_MODE_LABELS.get(mode, mode)}")

            attach, report, err = build_attachment(job, master, mode)
            if err:
                self._status(job["uid"], "Blocked: " + err[:60])
                self.progress("  " + err, level="error"); continue

            subject = job.get("email_subject") or ""
            body = job.get("email_body") or ""
            if not body.strip():
                made = ensure_job_email(job["uid"], ai=self.ai)
                if made:
                    subject, body = made
            if not body.strip():
                self._status(job["uid"], "No letter"); continue

            to = (job.get("contact_email") or "").strip()
            if not to:
                self._status(job["uid"], "CV ready (apply via link)")
                self.progress("  No email on this advert - open the link and apply on the site.", level="warn")
                continue

            if not pw:
                self._record(job, to, attach, subject, body, mode, "Prepared")
                self._status(job["uid"], "Prepared")
                continue

            if self.sent > 0 and delay:
                wait = delay + (random.randint(0, jitter) if jitter else 0)
                for _ in range(wait):
                    if self._cancel.is_set(): break
                    time.sleep(1)
                if self._cancel.is_set():
                    self.progress("Cancelled."); break

            try:
                msg = MIMEMultipart()
                msg["From"] = user
                msg["To"] = to
                msg["Subject"] = subject or f"Application for {job['title']}"
                msg.attach(MIMEText(body, "plain", "utf-8"))
                p = Path(attach)
                with open(p, "rb") as fh:
                    part = MIMEApplication(fh.read(), Name=p.name)
                part["Content-Disposition"] = f'attachment; filename="{p.name}"'
                msg.attach(part)

                ctx = ssl.create_default_context()
                if port == 587:
                    with smtplib.SMTP(host, port, timeout=45) as srv:
                        srv.starttls(context=ctx)
                        srv.login(user, pw)
                        srv.sendmail(user, to, msg.as_string())
                else:
                    with smtplib.SMTP_SSL(host, port, context=ctx, timeout=45) as srv:
                        srv.login(user, pw)
                        srv.sendmail(user, to, msg.as_string())

                self.sent += 1
                self._status(job["uid"], "Applied")
                self._record(job, to, attach, subject, body, mode, "Sent")
                self.progress(f"  Sent to {to}  ({self.sent} this run)")
            except smtplib.SMTPAuthenticationError:
                self.progress("  Login rejected. Gmail needs a 16-character App Password with 2-Step Verification on.", level="error")
                self._status(job["uid"], "Auth failed"); break
            except Exception as e:
                log("smtp: " + traceback.format_exc())
                self._status(job["uid"], "Send failed")
                self.progress(f"  Failed: {e}", level="error")

        self.progress(f"Finished. {self.sent} email(s) sent this run.", done=True)

    def _status(self, uid, status):
        with _db_lock:
            conn = get_db()
            conn.execute("UPDATE jobs SET status=? WHERE uid=?", (status, uid))
            conn.commit(); conn.close()

    def _record(self, job, to, attach, subject, body, mode, status):
        with _db_lock:
            conn = get_db()
            conn.execute(
                "INSERT INTO applications (job_uid, company, title, contact_email,"
                " cv_path, sent_at, status, cv_mode, email_subject, email_body)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (job["uid"], job.get("company") or "", job.get("title") or "", to,
                 str(attach), datetime.datetime.now().isoformat(), status,
                 CV_MODE_LABELS.get(mode, mode), subject, body))
            conn.commit(); conn.close()


# ==========================================================
# OPENING A PDF ON ANDROID
# ==========================================================
def open_file(path):
    path = str(path)
    try:
        from androidstorage4kivy import SharedStorage
        from android import mActivity
        from jnius import autoclass
        shared_uri = SharedStorage().copy_to_shared(path)
        Intent = autoclass('android.content.Intent')
        Uri = autoclass('android.net.Uri')
        intent = Intent(Intent.ACTION_VIEW)
        intent.setDataAndType(Uri.parse(str(shared_uri)), "application/pdf")
        intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        mActivity.startActivity(intent)
        return True, "Opened"
    except Exception as e:
        log(f"open_file: {e}")
        return False, f"Could not open it here. File saved at:\n{path}"
