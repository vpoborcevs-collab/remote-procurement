#!/usr/bin/env python3
"""
Daily remote procurement job scanner.
Sources: RemoteOK, Remotive, Jobicy, LinkedIn, EURES
Sends HTML email digest of new jobs only (deduplicates via seen_jobs.json).
"""

import json
import os
import re
import smtplib
import ssl
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SEEN_FILE      = Path(__file__).parent / "seen_jobs.json"
JOBS_FILE      = Path(__file__).parent / "jobs.json"
DISMISSED_FILE = Path(__file__).parent / "dismissed.json"

PROCUREMENT_KEYWORDS = [
    "procurement", "sourcing", "purchasing",
    "category manager", "category management",
    "supply chain",
    "vendor management", "supplier management",
    "indirect spend", "spend management",
    "head of procurement", "procurement director", "procurement lead",
    "chief procurement",
]

# Jobs with these location strings are treated as eligible even without
# explicit Europe mention (worldwide remote = can work from Latvia)
OPEN_LOCATIONS = {"", "worldwide", "remote", "anywhere", "global"}

EUROPE_KEYWORDS = [
    "europe", "european", "emea", "eu-", " eu ", "remote europe",
    "latvia", "estonia", "lithuania", "germany", "sweden", "denmark",
    "netherlands", "poland", "finland", "norway", "austria", "belgium",
    "switzerland", "czech", "hungary", "romania", "slovakia", "portugal",
    "france", "spain", "italy", "uk", "united kingdom", "ireland",
]


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def load_seen() -> set[str]:
    seen: set[str] = set()
    if SEEN_FILE.exists():
        seen |= set(json.loads(SEEN_FILE.read_text()))
    if DISMISSED_FILE.exists():
        seen |= set(json.loads(DISMISSED_FILE.read_text()))
    return seen


def save_seen(seen: set[str]) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=2))


def load_jobs() -> list[dict]:
    if JOBS_FILE.exists():
        return json.loads(JOBS_FILE.read_text())
    return []


def _is_expired(job: dict) -> bool:
    now = datetime.utcnow()
    # Explicit deadline from source (e.g. WeWorkRemotely)
    exp = job.get("expires_at")
    if exp:
        try:
            from email.utils import parsedate_to_datetime
            return parsedate_to_datetime(exp).replace(tzinfo=None) < now
        except Exception:
            pass
    # Fallback: remove after 30 days
    found = job.get("found_at", "")
    if found:
        try:
            age = now - datetime.strptime(found, "%Y-%m-%dT%H:%M:%SZ")
            return age.days > 30
        except Exception:
            pass
    return False


def save_jobs(new_jobs: list[dict]) -> None:
    existing = load_jobs()
    existing_ids = {j["id"] for j in existing}
    to_add = [j for j in new_jobs if j["id"] not in existing_ids]
    all_jobs = to_add + existing  # newest first
    # Remove expired postings
    before = len(all_jobs)
    all_jobs = [j for j in all_jobs if not _is_expired(j)]
    expired = before - len(all_jobs)
    if expired:
        print(f"  Removed {expired} expired posting(s)")
    JOBS_FILE.write_text(json.dumps(all_jobs, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def is_procurement_title(title: str, tags: str = "") -> bool:
    t = (title + " " + tags).lower()
    return any(kw in t for kw in PROCUREMENT_KEYWORDS)


def is_eligible_location(location: str) -> bool:
    loc = location.lower().strip()
    if loc in OPEN_LOCATIONS:
        return True
    return any(kw in loc for kw in EUROPE_KEYWORDS)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def fetch_remoteok() -> list[dict]:
    try:
        r = httpx.get(
            "https://remoteok.com/api",
            headers={"User-Agent": "Mozilla/5.0 (compatible; job-scanner/1.0)"},
            timeout=20,
            follow_redirects=True,
        )
        jobs = r.json()
        if isinstance(jobs, list) and jobs:
            jobs = jobs[1:]  # skip metadata header
        result = []
        for j in jobs:
            if not isinstance(j, dict):
                continue
            sal_min = j.get("salary_min") or j.get("salary", {}).get("min") if isinstance(j.get("salary"), dict) else j.get("salary_min")
            sal_max = j.get("salary_max") or j.get("salary", {}).get("max") if isinstance(j.get("salary"), dict) else j.get("salary_max")
            salary = None
            if sal_min and sal_max:
                salary = f"${int(sal_min)//1000}k–${int(sal_max)//1000}k/yr gross"
            elif sal_min:
                salary = f"${int(sal_min)//1000}k+/yr gross"
            result.append({
                "id": f"rok_{j.get('id', j.get('slug', ''))}",
                "title": j.get("position", ""),
                "company": j.get("company", ""),
                "location": j.get("location", "Worldwide"),
                "url": j.get("url", f"https://remoteok.com/remote-jobs/{j.get('slug','')}"),
                "tags": " ".join(j.get("tags", [])),
                "salary": salary,
                "source": "RemoteOK",
            })
        return result
    except Exception as exc:
        print(f"[RemoteOK] error: {exc}")
        return []


def fetch_remotive() -> list[dict]:
    try:
        r = httpx.get(
            "https://remotive.com/api/remote-jobs?limit=200",
            timeout=20,
            follow_redirects=True,
        )
        data = r.json()
        return [
            {
                "id": f"rem_{j['id']}",
                "title": j.get("title", ""),
                "company": j.get("company_name", ""),
                "location": j.get("candidate_required_location", ""),
                "url": j.get("url", ""),
                "tags": f"{j.get('category','')} {' '.join(j.get('tags',[]))}",
                "source": "Remotive",
            }
            for j in data.get("jobs", [])
        ]
    except Exception as exc:
        print(f"[Remotive] error: {exc}")
        return []


def fetch_jobicy() -> list[dict]:
    try:
        r = httpx.get(
            "https://jobicy.com/api/v2/remote-jobs?count=100&geo=europe",
            timeout=20,
            follow_redirects=True,
        )
        data = r.json()
        return [
            {
                "id": f"jcy_{j.get('id', '')}",
                "title": j.get("jobTitle", ""),
                "company": j.get("companyName", ""),
                "location": j.get("jobGeo", "Europe"),
                "url": j.get("url", ""),
                "tags": " ".join(j.get("jobIndustry", [])),
                "source": "Jobicy",
            }
            for j in data.get("jobs", [])
        ]
    except Exception as exc:
        print(f"[Jobicy] error: {exc}")
        return []


def fetch_linkedin() -> list[dict]:
    """LinkedIn guest jobs API — returns HTML, parsed with BeautifulSoup."""
    results: list[dict] = []
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    for keyword in ["procurement sourcing", "category manager", "purchasing manager"]:
        for start in [0, 25]:
            try:
                r = httpx.get(
                    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search",
                    params={
                        "keywords": keyword,
                        "location": "Europe",
                        "f_WT": "2",  # remote only
                        "start": str(start),
                    },
                    headers=headers,
                    timeout=20,
                    follow_redirects=True,
                )
                soup = BeautifulSoup(r.text, "lxml")
                for card in soup.select("li"):
                    title_el   = card.select_one("h3.base-search-card__title")
                    company_el = card.select_one("h4.base-search-card__subtitle")
                    loc_el     = card.select_one("span.job-search-card__location")
                    link_el    = card.select_one("a.base-card__full-link")
                    salary_el  = card.select_one("span.job-search-card__salary-info")
                    if not (title_el and link_el):
                        continue
                    url = link_el["href"].split("?")[0]
                    m = re.search(r"/jobs/view/(\d+)", url)
                    job_id = f"li_{m.group(1)}" if m else f"li_{abs(hash(url))}"
                    raw_salary = salary_el.get_text(strip=True) if salary_el else None
                    salary = f"{raw_salary} gross/mo" if raw_salary else None
                    results.append({
                        "id": job_id,
                        "title": title_el.get_text(strip=True),
                        "company": company_el.get_text(strip=True) if company_el else "",
                        "location": loc_el.get_text(strip=True) if loc_el else "Europe",
                        "url": url,
                        "tags": "",
                        "salary": salary,
                        "source": "LinkedIn",
                    })
            except Exception as exc:
                print(f"[LinkedIn] error (keyword={keyword}, start={start}): {exc}")
    return results


def fetch_arbeitnow() -> list[dict]:
    """Arbeitnow — European remote jobs, free public API."""
    try:
        r = httpx.get(
            "https://arbeitnow.com/api/job-board-api",
            timeout=20,
            follow_redirects=True,
        )
        data = r.json()
        return [
            {
                "id": f"arb_{j['slug']}",
                "title": j.get("title", ""),
                "company": j.get("company_name", ""),
                "location": j.get("location", "Europe"),
                "url": j.get("url", ""),
                "tags": " ".join(j.get("tags", [])),
                "source": "Arbeitnow",
            }
            for j in data.get("data", [])
        ]
    except Exception as exc:
        print(f"[Arbeitnow] error: {exc}")
        return []


def fetch_weworkremotely() -> list[dict]:
    """We Work Remotely RSS feed."""
    try:
        r = httpx.get(
            "https://weworkremotely.com/remote-jobs.rss",
            headers={"User-Agent": "Mozilla/5.0 (compatible; job-scanner/1.0)"},
            timeout=20,
            follow_redirects=True,
        )
        items = re.findall(r"<item>(.*?)</item>", r.text, re.DOTALL)
        results = []
        for item in items:
            def _extract(tag: str) -> str:
                m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", item, re.DOTALL)
                return re.sub(r"<!\[CDATA\[|\]\]>", "", m.group(1)).strip() if m else ""
            raw_title = _extract("title")
            parts = raw_title.split(": ", 1)
            company = parts[0] if len(parts) > 1 else ""
            title   = parts[1] if len(parts) > 1 else raw_title
            url     = _extract("link") or _extract("guid")
            results.append({
                "id":       f"wwr_{abs(hash(url))}",
                "title":    title,
                "company":    company,
                "location":   _extract("region") or "Worldwide",
                "url":        url,
                "tags":       _extract("category"),
                "expires_at": _extract("expires_at") or None,
                "source":     "WeWorkRemotely",
            })
        return results
    except Exception as exc:
        print(f"[WeWorkRemotely] error: {exc}")
        return []


def fetch_workingnomads() -> list[dict]:
    """Working Nomads public JSON API."""
    try:
        r = httpx.get(
            "https://www.workingnomads.com/api/exposed_jobs/",
            params={"limit": 200},
            timeout=20,
            follow_redirects=True,
        )
        data = r.json()
        return [
            {
                "id":       f"wn_{abs(hash(j.get('url', j.get('title', ''))))}",
                "title":    j.get("title", ""),
                "company":  j.get("company_name", ""),
                "location": j.get("location", "Remote"),
                "url":      j.get("url", ""),
                "tags":     f"{j.get('category_name', '')} {j.get('tags', '')}",
                "source":   "WorkingNomads",
            }
            for j in data
            if isinstance(j, dict)
        ]
    except Exception as exc:
        print(f"[WorkingNomads] error: {exc}")
        return []


def fetch_euremotejobs() -> list[dict]:
    """EU Remote Jobs WordPress REST API (European-focused remote jobs)."""
    results: list[dict] = []
    for page in range(1, 4):
        try:
            r = httpx.get(
                "https://euremotejobs.com/wp-json/wp/v2/job-listings",
                params={"per_page": 50, "page": page},
                timeout=20,
                follow_redirects=True,
            )
            if r.status_code != 200:
                break
            data = r.json()
            if not data:
                break
            for j in data:
                meta    = j.get("meta", {})
                company = meta.get("_company_name", "")
                title   = re.sub(r"&#\d+;|&\w+;|<[^>]+>", "", j.get("title", {}).get("rendered", "")).strip()
                results.append({
                    "id":       f"eu_{j['id']}",
                    "title":    title,
                    "company":  company,
                    "location": "Europe",
                    "url":      j.get("link", ""),
                    "tags":     "",
                    "source":   "EURemoteJobs",
                })
        except Exception as exc:
            print(f"[EURemoteJobs] error (page={page}): {exc}")
            break
    return results


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

def scan() -> list[dict]:
    print(f"[{datetime.utcnow():%Y-%m-%d %H:%M}] Fetching jobs...")

    all_jobs: list[dict] = []
    all_jobs.extend(fetch_remoteok())
    all_jobs.extend(fetch_remotive())
    all_jobs.extend(fetch_jobicy())
    all_jobs.extend(fetch_linkedin())
    all_jobs.extend(fetch_arbeitnow())
    all_jobs.extend(fetch_weworkremotely())
    all_jobs.extend(fetch_workingnomads())
    all_jobs.extend(fetch_euremotejobs())

    # Cross-source deduplication: same URL or same title+company
    seen_urls: set[str] = set()
    seen_slugs: set[str] = set()
    deduped: list[dict] = []
    for j in all_jobs:
        url_key  = j.get("url", "").split("?")[0].rstrip("/").lower()
        slug_key = (j.get("title", "").lower().strip() + "|" + j.get("company", "").lower().strip())
        if url_key and url_key in seen_urls:
            continue
        if slug_key and slug_key != "|" and slug_key in seen_slugs:
            continue
        if url_key:
            seen_urls.add(url_key)
        if slug_key != "|":
            seen_slugs.add(slug_key)
        deduped.append(j)
    all_jobs = deduped
    print(f"  Total fetched: {len(all_jobs)}")

    # Filter: procurement role + eligible location
    relevant = [
        j for j in all_jobs
        if is_procurement_title(j["title"], j.get("tags", ""))
        and is_eligible_location(j["location"])
    ]
    print(f"  Relevant (procurement + location): {len(relevant)}")

    # Deduplicate
    seen = load_seen()
    new_jobs: list[dict] = []
    seen_this_run: set[str] = set()
    for j in relevant:
        if j["id"] not in seen and j["id"] not in seen_this_run:
            new_jobs.append(j)
            seen_this_run.add(j["id"])

    print(f"  New (not seen before): {len(new_jobs)}")

    # Stamp found_at
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    for j in new_jobs:
        j["found_at"] = now

    # Persist
    save_seen(seen | seen_this_run)
    save_jobs(new_jobs)

    return new_jobs


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def build_html(jobs: list[dict]) -> str:
    date_str = datetime.utcnow().strftime("%B %d, %Y")

    if not jobs:
        body = "<p style='color:#64748b;'>No new remote procurement positions found today.</p>"
    else:
        rows = ""
        for j in jobs:
            rows += f"""
            <tr>
              <td style="padding:16px 0;border-bottom:1px solid #e2e8f0;">
                <a href="{j['url']}" style="font-size:16px;font-weight:600;color:#1e3a8a;text-decoration:none;">
                  {j['title']}
                </a><br>
                <span style="font-size:14px;color:#374151;">{j['company']}</span>
                &nbsp;·&nbsp;
                <span style="font-size:13px;color:#6b7280;">{j['location']}</span>
                &nbsp;·&nbsp;
                <span style="font-size:11px;background:#f1f5f9;color:#64748b;
                             padding:2px 8px;border-radius:4px;">{j['source']}</span>
              </td>
            </tr>"""

        body = f"""
        <p style="color:#374151;margin-bottom:20px;">
          Found <strong>{len(jobs)} new position{"s" if len(jobs)!=1 else ""}</strong>
          matching remote procurement in Europe.
        </p>
        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
          {rows}
        </table>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <div style="max-width:640px;margin:40px auto;background:#ffffff;border-radius:8px;
              box-shadow:0 1px 4px rgba(0,0,0,0.08);overflow:hidden;">
    <div style="background:#0a1230;padding:28px 32px;">
      <p style="margin:0;font-size:11px;font-weight:600;letter-spacing:0.1em;
                text-transform:uppercase;color:#f59e0b;">Daily Job Scan</p>
      <h1 style="margin:6px 0 0;font-size:22px;color:#ffffff;font-weight:700;">
        Remote Procurement — {date_str}
      </h1>
    </div>
    <div style="padding:28px 32px;">
      {body}
    </div>
    <div style="padding:16px 32px;background:#f8fafc;border-top:1px solid #e2e8f0;
                font-size:11px;color:#9ca3af;">
      Sources: RemoteOK · Remotive · Jobicy · LinkedIn · Arbeitnow · WeWorkRemotely · WorkingNomads · EURemoteJobs &nbsp;|&nbsp;
      Keywords: procurement, sourcing, category management, indirect
    </div>
  </div>
</body>
</html>"""


def send_email(jobs: list[dict]) -> None:
    user = os.environ["EMAIL_USER"]
    password = os.environ["EMAIL_APP_PASSWORD"]
    to_addr = os.environ.get("EMAIL_TO", user)

    subject = (
        f"[Job Scan] {len(jobs)} new remote procurement position{'s' if len(jobs)!=1 else ''}"
        if jobs
        else "[Job Scan] No new positions today"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    msg.attach(MIMEText(build_html(jobs), "html"))

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as smtp:
        smtp.login(user, password)
        smtp.sendmail(user, to_addr, msg.as_string())

    print(f"  Email sent → {to_addr}  (subject: {subject})")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    new_jobs = scan()

    if os.environ.get("EMAIL_USER"):
        send_email(new_jobs)
    else:
        print("  EMAIL_USER not set — skipping email. Found jobs:")
        for j in new_jobs:
            print(f"    [{j['source']}] {j['title']} @ {j['company']} — {j['url']}")
