"""
JobThai Plugin API — FastAPI Backend
รองรับ Coze Plugin Spec: openapi_spec.yaml
Deploy บน Render.com (Free Tier)
"""

from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import httpx
from bs4 import BeautifulSoup
from datetime import datetime, date
import re
import asyncio

app = FastAPI(
    title="JobThai Job Search Plugin",
    description="Coze Plugin สำหรับค้นหางานจาก JobThai",
    version="1.0.0",
)

# อนุญาต CORS สำหรับ Coze
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================
# HEADERS สำหรับ Request ไป JobThai
# ==========================================
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "th-TH,th;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.jobthai.com/",
}

JOBTHAI_BASE = "https://www.jobthai.com"

# ==========================================
# JOB TYPE MAPPING
# ==========================================
JOB_TYPE_MAP = {
    "all":        "",
    "fulltime":   "1",
    "parttime":   "2",
    "internship": "3",   # ตรวจสอบ parameter จริงของ JobThai
    "freelance":  "4",
}


# ==========================================
# PYDANTIC SCHEMAS
# ==========================================
class MatchRequest(BaseModel):
    skills: list[str]
    education_level: Optional[str] = "any"
    preferred_location: Optional[str] = None
    job_type: Optional[str] = "all"
    experience_years: Optional[int] = 0
    limit: Optional[int] = 10


# ==========================================
# HELPER FUNCTIONS
# ==========================================
def parse_job_card(card) -> dict:
    """แปลง HTML card element เป็น dict"""
    try:
        # ชื่อตำแหน่ง
        title_el = card.select_one("a.job-title, h3.title, .position-name")
        title = title_el.get_text(strip=True) if title_el else "ไม่ระบุ"

        # ลิงก์
        link_el = card.select_one("a[href*='/th/job/']")
        job_url = JOBTHAI_BASE + link_el["href"] if link_el else ""
        job_id = re.search(r"/job/(\d+)", job_url)
        job_id = job_id.group(1) if job_id else ""

        # บริษัท
        company_el = card.select_one(".company-name, .employer-name, span.company")
        company = company_el.get_text(strip=True) if company_el else "ไม่ระบุ"

        # ที่ตั้ง
        location_el = card.select_one(".location, .job-location, span.province")
        location = location_el.get_text(strip=True) if location_el else "ไม่ระบุ"

        # เงินเดือน
        salary_el = card.select_one(".salary, .wage, span.salary")
        salary = salary_el.get_text(strip=True) if salary_el else "ตามตกลง"

        # วันที่ประกาศ
        date_el = card.select_one(".post-date, .date, time")
        posted_date = date_el.get_text(strip=True) if date_el else ""

        # ทักษะที่ต้องการ (บางครั้งแสดงใน tag)
        skill_els = card.select(".skill-tag, .tag, .badge-skill")
        required_skills = [s.get_text(strip=True) for s in skill_els]

        return {
            "job_id": job_id,
            "title": title,
            "company": company,
            "location": location,
            "salary": salary,
            "posted_date": posted_date,
            "deadline": None,
            "is_active": True,
            "required_skills": required_skills,
            "url": job_url,
            "job_type": "unknown",
        }
    except Exception:
        return None


def calculate_match_score(user_skills: list[str], job: dict) -> dict:
    """คำนวณคะแนน match ระหว่างทักษะผู้ใช้กับ Job"""
    user_skills_lower = [s.lower().strip() for s in user_skills]
    required_lower = [s.lower().strip() for s in job.get("required_skills", [])]

    if not required_lower:
        # ถ้างานไม่ระบุทักษะ ใช้ title matching แทน
        title_lower = job.get("title", "").lower()
        matched = [s for s in user_skills_lower if s in title_lower]
        score = min(len(matched) / max(len(user_skills_lower), 1), 1.0)
        missing = []
    else:
        matched = [s for s in user_skills_lower if s in required_lower]
        missing = [s for s in required_lower if s not in user_skills_lower]
        score = len(matched) / max(len(required_lower), 1)

    # ปรับ score ด้วย title relevance
    title_lower = job.get("title", "").lower()
    title_bonus = sum(0.05 for s in user_skills_lower if s in title_lower)
    score = min(score + title_bonus, 1.0)

    # สร้าง reason
    if matched:
        reason = f"ทักษะที่ตรงกัน: {', '.join(matched[:3])}"
        if missing:
            reason += f" | ทักษะที่ยังขาด: {', '.join(missing[:2])}"
    else:
        reason = "อาจเหมาะสม — ตรวจสอบรายละเอียดเพิ่มเติม"

    return {
        **job,
        "match_score": round(score, 2),
        "match_percentage": int(score * 100),
        "matched_skills": [s for s in user_skills if s.lower() in required_lower],
        "missing_skills": [s for s in job.get("required_skills", [])
                           if s.lower() not in user_skills_lower],
        "match_reason": reason,
    }


# ==========================================
# ROUTES
# ==========================================

@app.get("/health")
async def health_check():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


@app.get("/search-jobs")
async def search_jobs(
    keyword: str = Query(..., min_length=2, description="คำค้นหา"),
    job_type: str = Query("all", description="ประเภทงาน"),
    location: Optional[str] = Query(None, description="จังหวัด"),
    skills: Optional[str] = Query(None, description="ทักษะคั่นด้วยคอมมา"),
    limit: int = Query(10, ge=1, le=20, description="จำนวนผลลัพธ์"),
):
    """ค้นหางานจาก JobThai"""
    # สร้าง URL params
    params = {"keyword": keyword}
    if job_type and job_type != "all":
        params["jobtype"] = JOB_TYPE_MAP.get(job_type, "")
    if location:
        params["province"] = location

    search_url = f"{JOBTHAI_BASE}/th/jobs"

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(search_url, params=params, headers=HEADERS)
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"JobThai ตอบกลับ: {e.response.status_code}")
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="ไม่สามารถเชื่อมต่อ JobThai ได้")

    soup = BeautifulSoup(resp.text, "html.parser")

    # หา job cards (selector อาจต้องปรับตาม HTML จริงของ JobThai)
    job_cards = soup.select(
        "div.job-list-item, article.job-card, li.job-item, div[data-jobid]"
    )

    jobs = []
    for card in job_cards[:limit]:
        parsed = parse_job_card(card)
        if parsed:
            jobs.append(parsed)

    # ถ้า skills ถูกส่งมา ให้ sort by match score
    if skills:
        skill_list = [s.strip() for s in skills.split(",")]
        jobs = sorted(
            [calculate_match_score(skill_list, j) for j in jobs],
            key=lambda x: x.get("match_score", 0),
            reverse=True,
        )

    return {
        "total_found": len(job_cards),
        "returned_count": len(jobs),
        "search_keyword": keyword,
        "jobs": jobs,
    }


@app.post("/match-jobs")
async def match_jobs(body: MatchRequest):
    """จับคู่งานกับทักษะของผู้ใช้"""
    if not body.skills:
        raise HTTPException(status_code=400, detail="กรุณาระบุทักษะอย่างน้อย 1 รายการ")

    # ใช้ทักษะแรกๆ เป็น keyword สำหรับ search
    keyword = " ".join(body.skills[:3])
    job_type = body.job_type or "all"
    location = body.preferred_location

    params = {"keyword": keyword}
    if job_type != "all":
        params["jobtype"] = JOB_TYPE_MAP.get(job_type, "")
    if location:
        params["province"] = location

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{JOBTHAI_BASE}/th/jobs", params=params, headers=HEADERS
            )
            resp.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))

    soup = BeautifulSoup(resp.text, "html.parser")
    job_cards = soup.select(
        "div.job-list-item, article.job-card, li.job-item, div[data-jobid]"
    )

    jobs_raw = [parse_job_card(c) for c in job_cards[: body.limit * 2]]
    jobs_raw = [j for j in jobs_raw if j]

    # คำนวณ match score
    matches = [calculate_match_score(body.skills, j) for j in jobs_raw]
    matches.sort(key=lambda x: x["match_score"], reverse=True)
    matches = matches[: body.limit]

    return {
        "matched_count": len(matches),
        "user_skills": body.skills,
        "matches": matches,
    }


@app.get("/job-detail")
async def get_job_detail(job_id: str = Query(..., description="Job ID จาก JobThai")):
    """ดึงรายละเอียดงานเต็ม"""
    job_url = f"{JOBTHAI_BASE}/th/job/{job_id}"

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(job_url, headers=HEADERS)
            if resp.status_code == 404:
                raise HTTPException(status_code=404, detail="ไม่พบงานนี้ หรืองานปิดรับสมัครแล้ว")
            resp.raise_for_status()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))

    soup = BeautifulSoup(resp.text, "html.parser")

    # ดึง meta fields (selectors ต้องปรับตาม HTML จริง)
    def get_text(selector):
        el = soup.select_one(selector)
        return el.get_text(strip=True) if el else None

    def get_list(selector):
        return [el.get_text(strip=True) for el in soup.select(selector)]

    title = get_text("h1.position-name, h1.job-title, h1")
    company = get_text(".company-name, .employer")
    location = get_text(".location, .province")
    salary = get_text(".salary")
    description = get_text(".job-description, .description, #job-detail")
    responsibilities = get_list(".responsibilities li, .duty li")
    qualifications = get_list(".qualifications li, .requirement li")
    benefits = get_list(".benefit li, .welfare li")
    work_hours = get_text(".work-hour, .working-hour")
    skills_els = get_list(".skill-tag, .required-skill span")

    return {
        "job_id": job_id,
        "title": title or "ไม่ระบุ",
        "company": company or "ไม่ระบุ",
        "location": location or "ไม่ระบุ",
        "salary": salary or "ตามตกลง",
        "posted_date": None,
        "deadline": None,
        "is_active": True,
        "required_skills": skills_els,
        "url": job_url,
        "job_type": "unknown",
        "description": description,
        "responsibilities": responsibilities,
        "qualifications": qualifications,
        "benefits": benefits,
        "work_hours": work_hours,
        "internship_duration": None,
        "contact_email": None,
        "company_info": {"name": company},
    }
