"""
Briefing App — Backend
FastAPI server handling Gmail OAuth, article fetching,
briefing generation, and delivery via email + WhatsApp.
"""

import os
import json
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv

from services.gmail import GmailService
from services.claude import ClaudeService
from services.delivery import DeliveryService
from services.scheduler import BriefingScheduler

load_dotenv()

app = FastAPI(title="Briefing API", version="1.0.0")

# ── CORS (allow your frontend origin) ─────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Tighten this to your frontend URL in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Services ───────────────────────────────────────────────────────────────────
gmail = GmailService(
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    redirect_uri=os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/auth/callback"),
)
claude_svc = ClaudeService(api_key=os.getenv("ANTHROPIC_API_KEY"))
delivery = DeliveryService(
    sendgrid_key=os.getenv("SENDGRID_API_KEY"),
    twilio_sid=os.getenv("TWILIO_ACCOUNT_SID"),
    twilio_token=os.getenv("TWILIO_AUTH_TOKEN"),
    twilio_whatsapp_from=os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886"),
    from_email=os.getenv("FROM_EMAIL", "briefing@yourdomain.com"),
)
scheduler = BriefingScheduler()

# ── Simple file-based storage (swap for Postgres in prod) ─────────────────────
STORAGE_FILE = "data/app_data.json"

def load_data() -> dict:
    os.makedirs("data", exist_ok=True)
    if not os.path.exists(STORAGE_FILE):
        return {"profiles": [], "user_profile": {}, "gmail_token": None}
    with open(STORAGE_FILE) as f:
        return json.load(f)

def save_data(data: dict):
    os.makedirs("data", exist_ok=True)
    with open(STORAGE_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ── Pydantic models ────────────────────────────────────────────────────────────
class UserProfile(BaseModel):
    name: str = ""
    role: str = ""
    context: str = ""
    interests: str = ""

class BriefingProfile(BaseModel):
    id: str
    name: str
    context: str = ""
    tone: str = "analytical"
    length: str = "standard"
    sources: list[str] = []
    schedule: str = "manual"
    scheduleTime: str = "07:00"
    delivery: list[str] = ["app"]
    email: str = ""
    whatsapp: str = ""
    scope: str = "email"

class GenerateRequest(BaseModel):
    profile_id: str
    articles: Optional[list[dict]] = None  # If None, fetch from Gmail

class DeliverRequest(BaseModel):
    profile_id: str
    script: str


# ── Health ─────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {
        "status": "ok",
        "time": datetime.utcnow().isoformat(),
        "redirect_uri": os.getenv("GOOGLE_REDIRECT_URI", "NOT SET"),
    }


# ── Gmail Auth ─────────────────────────────────────────────────────────────────
@app.get("/auth/gmail")
def gmail_auth():
    """Step 1: Redirect user to Google OAuth consent screen."""
    auth_url = gmail.get_auth_url()
    return RedirectResponse(auth_url)

@app.get("/auth/callback")
def gmail_callback(code: str, request: Request):
    """Step 2: Google redirects here with auth code. Exchange for tokens."""
    try:
        token = gmail.exchange_code(code)
        data = load_data()
        data["gmail_token"] = token
        save_data(data)
        # Redirect back to the frontend
        frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
        return RedirectResponse(f"{frontend_url}?gmail=connected")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/auth/status")
def auth_status():
    """Check if Gmail is connected."""
    data = load_data()
    return {"gmail_connected": data.get("gmail_token") is not None}

@app.delete("/auth/gmail")
def gmail_disconnect():
    """Disconnect Gmail."""
    data = load_data()
    data["gmail_token"] = None
    save_data(data)
    return {"disconnected": True}


# ── Articles ───────────────────────────────────────────────────────────────────
@app.get("/articles")
def get_articles(hours: int = 24, max_results: int = 20):
    """
    Fetch newsletter emails from Gmail from the last N hours.
    Returns structured article objects ready for the frontend.
    """
    data = load_data()
    token = data.get("gmail_token")
    if not token:
        raise HTTPException(status_code=401, detail="Gmail not connected. Visit /auth/gmail first.")

    try:
        articles = gmail.fetch_newsletters(token, hours=hours, max_results=max_results),  # reads your specific folder)
        # Refresh token if it was updated
        if gmail.last_refreshed_token:
            data["gmail_token"] = gmail.last_refreshed_token
            save_data(data)
        return {"articles": articles, "count": len(articles), "fetched_at": datetime.utcnow().isoformat()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gmail fetch error: {str(e)}")


# ── Profiles ───────────────────────────────────────────────────────────────────
@app.get("/profiles")
def get_profiles():
    data = load_data()
    return {
        "user_profile": data.get("user_profile", {}),
        "briefing_profiles": data.get("profiles", []),
    }

@app.post("/profiles")
def save_profiles(user_profile: UserProfile, briefing_profiles: list[BriefingProfile]):
    data = load_data()
    data["user_profile"] = user_profile.dict()
    data["profiles"] = [p.dict() for p in briefing_profiles]
    save_data(data)
    # Update scheduler with new profiles
    _sync_scheduler(data)
    return {"saved": True}

@app.put("/profiles/user")
def update_user_profile(profile: UserProfile):
    data = load_data()
    data["user_profile"] = profile.dict()
    save_data(data)
    return {"saved": True}


# ── Briefing Generation ────────────────────────────────────────────────────────
@app.post("/briefing/generate")
async def generate_briefing(req: GenerateRequest):
    """
    Generate a briefing script using Claude.
    Optionally fetches articles from Gmail if none are provided.
    """
    data = load_data()
    profiles = data.get("profiles", [])
    user_profile = data.get("user_profile", {})

    profile = next((p for p in profiles if p["id"] == req.profile_id), None)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    # Fetch articles from Gmail if not provided
    articles = req.articles
    if not articles:
        token = data.get("gmail_token")
        if not token:
            raise HTTPException(status_code=401, detail="Gmail not connected")
        articles = gmail.fetch_newsletters(token, hours=24, max_results=20)
        # Filter by profile sources
        if profile.get("sources"):
            articles = [a for a in articles if a.get("source") in profile["sources"]]

    if not articles:
        raise HTTPException(status_code=404, detail="No articles found for this period")

    try:
        script = await claude_svc.generate_briefing(
            articles=articles,
            user_profile=user_profile,
            briefing_profile=profile,
        )
        return {
            "script": script,
            "article_count": len(articles),
            "profile_name": profile["name"],
            "generated_at": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Generation error: {str(e)}")


# ── Delivery ───────────────────────────────────────────────────────────────────
@app.post("/briefing/deliver")
async def deliver_briefing(req: DeliverRequest):
    """
    Deliver a generated briefing script via configured channels
    (email and/or WhatsApp).
    """
    data = load_data()
    profiles = data.get("profiles", [])
    profile = next((p for p in profiles if p["id"] == req.profile_id), None)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    results = {}

    # Email delivery
    if "email" in profile.get("delivery", []) and profile.get("email"):
        try:
            delivery.send_email(
                to_email=profile["email"],
                subject=f"Your {profile['name']} — {datetime.now().strftime('%A %d %B')}",
                body_text=req.script,
                body_html=_script_to_html(req.script, profile["name"]),
            )
            results["email"] = {"status": "sent", "to": profile["email"]}
        except Exception as e:
            results["email"] = {"status": "failed", "error": str(e)}

    # WhatsApp delivery
    if "whatsapp" in profile.get("delivery", []) and profile.get("whatsapp"):
        try:
            # WhatsApp has a 1600 char limit — send a summary
            short_script = req.script[:1400] + "..." if len(req.script) > 1400 else req.script
            delivery.send_whatsapp(
                to_number=profile["whatsapp"],
                message=f"*{profile['name']}*\n_{datetime.now().strftime('%A %d %B')}_\n\n{short_script}",
            )
            results["whatsapp"] = {"status": "sent", "to": profile["whatsapp"]}
        except Exception as e:
            results["whatsapp"] = {"status": "failed", "error": str(e)}

    return {"delivery_results": results, "delivered_at": datetime.utcnow().isoformat()}


# ── Scheduler ──────────────────────────────────────────────────────────────────
@app.get("/scheduler/status")
def scheduler_status():
    return {"jobs": scheduler.list_jobs()}

def _sync_scheduler(data: dict):
    """Update scheduler based on current profiles."""
    scheduler.clear_all()
    profiles = data.get("profiles", [])
    for profile in profiles:
        if profile.get("schedule") == "manual":
            continue
        scheduler.add_job(
            profile_id=profile["id"],
            schedule=profile["schedule"],
            time_str=profile.get("scheduleTime", "07:00"),
            callback=_scheduled_run,
        )

async def _scheduled_run(profile_id: str):
    """Called by scheduler — generate and deliver a briefing."""
    data = load_data()
    profiles = data.get("profiles", [])
    user_profile = data.get("user_profile", {})
    profile = next((p for p in profiles if p["id"] == profile_id), None)
    if not profile:
        return

    token = data.get("gmail_token")
    if not token:
        print(f"[Scheduler] No Gmail token for profile {profile_id}")
        return

    articles = gmail.fetch_newsletters(token, hours=24, max_results=20)
    if profile.get("sources"):
        articles = [a for a in articles if a.get("source") in profile["sources"]]

    if not articles:
        print(f"[Scheduler] No articles for profile {profile_id}")
        return

    script = await claude_svc.generate_briefing(articles, user_profile, profile)

    # Deliver
    if "email" in profile.get("delivery", []) and profile.get("email"):
        delivery.send_email(
            to_email=profile["email"],
            subject=f"Your {profile['name']} — {datetime.now().strftime('%A %d %B')}",
            body_text=script,
            body_html=_script_to_html(script, profile["name"]),
        )
    if "whatsapp" in profile.get("delivery", []) and profile.get("whatsapp"):
        short = script[:1400] + "..." if len(script) > 1400 else script
        delivery.send_whatsapp(
            to_number=profile["whatsapp"],
            message=f"*{profile['name']}*\n_{datetime.now().strftime('%A %d %B')}_\n\n{short}",
        )
    print(f"[Scheduler] Briefing delivered for profile: {profile['name']}")


# ── HTML email template ────────────────────────────────────────────────────────
def _script_to_html(script: str, title: str) -> str:
    paragraphs = "".join(f"<p>{p}</p>" for p in script.split("\n\n") if p.strip())
    date_str = datetime.now().strftime("%A %d %B %Y")
    return f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: Georgia, serif; max-width: 640px; margin: 0 auto; padding: 40px 20px; background: #ffffff; color: #1a1a1a; }}
  .header {{ border-bottom: 3px solid #FF6719; padding-bottom: 20px; margin-bottom: 32px; }}
  .title {{ font-size: 28px; font-weight: bold; margin: 0 0 6px; }}
  .date {{ font-family: monospace; font-size: 12px; color: #888; letter-spacing: 0.1em; }}
  p {{ font-size: 16px; line-height: 1.85; margin-bottom: 20px; color: #2a2a2a; }}
  .footer {{ margin-top: 40px; padding-top: 20px; border-top: 1px solid #eee; font-family: monospace; font-size: 11px; color: #aaa; }}
</style>
</head>
<body>
  <div class="header">
    <div class="title">{title}</div>
    <div class="date">{date_str.upper()}</div>
  </div>
  {paragraphs}
  <div class="footer">AI-generated briefing · Powered by Claude · Briefing App</div>
</body>
</html>
"""


# ── Startup ────────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    scheduler.start()
    data = load_data()
    _sync_scheduler(data)
    print("✓ Briefing API started")

@app.on_event("shutdown")
async def shutdown():
    scheduler.stop()
