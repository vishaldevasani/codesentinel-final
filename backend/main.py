"""
AI-Powered Code Review Assistant — FastAPI Backend
Uses FREE AI APIs: Groq (Llama 3.3 70B) or Google Gemini 2.5 Flash
No paid API keys required!
"""

import os
import hmac
import hashlib
import json
import time
from datetime import datetime
from typing import Optional

import httpx
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="CodeSentinel — AI Code Review", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── In-memory store ──────────────────────────────────────────────
pr_store: dict = {}
review_stats: dict = {
    "total_prs": 0,
    "total_issues": 0,
    "issues_by_severity": {"critical": 0, "high": 0, "medium": 0, "low": 0},
    "issues_by_type": {"bug": 0, "security": 0, "performance": 0, "style": 0},
}

# ── Config ───────────────────────────────────────────────────────
GITHUB_TOKEN          = os.getenv("GITHUB_TOKEN", "")
GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")

# Free AI API keys — pick one or both (Groq preferred, Gemini fallback)
GROQ_API_KEY   = os.getenv("GROQ_API_KEY", "")    # https://console.groq.com  (FREE)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")  # https://aistudio.google.com (FREE)

# ── Models ───────────────────────────────────────────────────────
class ReviewIssue(BaseModel):
    type: str        # bug | security | performance | style
    severity: str    # critical | high | medium | low
    file: str
    line: Optional[int] = None
    title: str
    description: str
    suggestion: str

class PRReview(BaseModel):
    pr_id: str
    repo: str
    title: str
    author: str
    url: str
    status: str
    issues: list[ReviewIssue] = []
    summary: str = ""
    score: int = 100
    analyzed_at: Optional[str] = None
    files_changed: int = 0
    additions: int = 0
    deletions: int = 0
    ai_provider: str = "demo"

# ── HMAC verification ────────────────────────────────────────────
def verify_github_signature(payload: bytes, signature: str) -> bool:
    if not GITHUB_WEBHOOK_SECRET:
        return True
    mac = hmac.new(GITHUB_WEBHOOK_SECRET.encode(), msg=payload, digestmod=hashlib.sha256)
    return hmac.compare_digest(f"sha256={mac.hexdigest()}", signature)

# ── GitHub API helpers ───────────────────────────────────────────
async def fetch_pr_diff(owner: str, repo: str, pr_number: int) -> tuple[str, dict]:
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    async with httpx.AsyncClient() as client:
        meta = (await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
            headers=headers, timeout=30
        )).json()
        diff = (await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
            headers={**headers, "Accept": "application/vnd.github.v3.diff"},
            timeout=30
        )).text
    return diff, meta

async def post_github_review(owner: str, repo: str, pr_number: int,
                              issues: list, summary: str, score: int,
                              commit_sha: str):
    if not GITHUB_TOKEN:
        return
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    icons = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}
    type_icons = {"bug": "🐛", "security": "🔒", "performance": "⚡", "style": "💅"}

    comments = [
        {
            "path": i["file"],
            "line": i["line"],
            "body": (
                f"{type_icons.get(i['type'],'ℹ️')} **[{i['severity'].upper()}]** **{i['title']}**\n\n"
                f"{i['description']}\n\n**💡 Suggestion:** {i['suggestion']}"
            )
        }
        for i in issues if i.get("line")
    ][:10]

    rows = "\n".join(
        f"| {type_icons.get(i['type'],'ℹ️')} {i['type'].title()} "
        f"| {icons.get(i['severity'],'')} {i['severity'].title()} "
        f"| `{i['file']}` | {i['title']} |"
        for i in issues
    ) or "| — | — | — | No issues found ✅ |"

    verdict = "APPROVE" if score >= 85 else ("REQUEST_CHANGES" if score < 60 else "COMMENT")

    async with httpx.AsyncClient() as client:
        await client.post(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
            headers=headers,
            json={
                "commit_id": commit_sha,
                "body": f"## 🤖 AI Code Review — Score: {score}/100\n\n{summary}\n\n"
                        f"### Issues ({len(issues)})\n\n"
                        f"| Type | Severity | File | Issue |\n|------|----------|------|-------|\n{rows}\n\n"
                        f"*Powered by CodeSentinel (Groq Llama / Google Gemini)*",
                "event": verdict,
                "comments": comments,
            },
            timeout=30
        )

# ── AI Analysis — Groq (FREE, ultra-fast) ───────────────────────
async def analyze_with_groq(diff: str, pr_title: str) -> tuple[list, str, int, str]:
    """
    Groq free tier: https://console.groq.com
    Model: llama-3.3-70b-versatile — FREE, 315 tokens/sec
    Free limits: 6,000 req/day, 500K tokens/min
    """
    prompt = build_review_prompt(diff, pr_title)
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": 4096,
            },
            timeout=60
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"]
    result = parse_ai_response(raw)

issues = result["issues"]
summary = result["summary"]
score = result["score"]
    return issues, summary, score, "groq/llama-3.3-70b"

# ── AI Analysis — Google Gemini (FREE) ──────────────────────────
async def analyze_with_gemini(diff: str, pr_title: str) -> tuple[list, str, int, str]:
    """
    Google AI Studio free tier: https://aistudio.google.com
    Model: gemini-2.0-flash — FREE, 1500 req/day
    No credit card required.
    """
    prompt = build_review_prompt(diff, pr_title)
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}",
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.2, "maxOutputTokens": 4096},
            },
            timeout=60
        )
        resp.raise_for_status()
        raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
   result = parse_ai_response(raw)

    issues = result["issues"]
    summary = result["summary"]
    score = result["score"]
    return issues, summary, score, "google/gemini-2.0-flash"

# ── Shared prompt + parser ───────────────────────────────────────
def build_review_prompt(diff: str, pr_title: str) -> str:
    return f"""You are a senior staff software engineer and security reviewer.

Analyze this GitHub Pull Request deeply and find ALL possible issues.

PR Title:
{pr_title}

Code Diff:
{diff[:15000]}

Review for these categories:

1. Security vulnerabilities
- Hardcoded secrets
- API keys
- Passwords
- SQL injection
- Command injection
- Unsafe eval/exec
- Sensitive data exposure
- Authentication flaws
- Missing validation
- Webhook bypasses

2. Bugs
- Null pointer issues
- Division by zero
- Wrong logic
- Incorrect conditions
- Runtime crashes
- Infinite loops
- Memory leaks
- Exception handling mistakes

3. Performance problems
- Inefficient loops
- O(n²) complexity
- Memory waste
- Large allocations
- Repeated computation
- Blocking operations

4. Code quality / style
- Bad naming
- Duplicate code
- Dead code
- Unused variables
- Formatting problems
- Missing error handling

5. Maintainability
- Poor structure
- Missing comments
- Magic numbers
- Repeated logic

For every issue provide:

- category
- severity
- file
- line
- title
- description
- suggestion

Return STRICT JSON ONLY.

Return EXACTLY this structure:

{{
  "issues": [
    {{
      "category": "security|bug|performance|style|maintainability",
      "severity": "critical|high|medium|low",
      "file": "path/to/file.py",
      "line": 42,
      "title": "Short issue title",
      "description": "Why this is a problem",
      "suggestion": "Concrete fix"
    }}
  ],
  "summary": "2-3 sentence overall assessment",
  "score": 78
}}

Be extremely strict and catch ALL possible issues visible in the diff.

ONLY report issues visible in the provided diff.
DO NOT invent files, functions, or line numbers.
If unsure, do not report it.
Use ONLY filenames present in the diff.

Only return valid JSON.
"""

# ── Demo fallback (no API key) ───────────────────────────────────
def demo_analysis() -> tuple[list, str, int, str]:
    issues = [
        {"type": "security", "severity": "critical", "file": "app/auth.py", "line": 34,
         "title": "SQL Injection Vulnerability",
         "description": "User input is concatenated directly into the SQL query without sanitization, allowing an attacker to manipulate the query.",
         "suggestion": "Use parameterized queries: cursor.execute('SELECT * FROM users WHERE id = %s', (user_id,))"},
        {"type": "security", "severity": "high", "file": "config/settings.py", "line": 12,
         "title": "Hardcoded Secret Key",
         "description": "A secret key is hardcoded in source code and will be committed to version control, exposing it to all contributors.",
         "suggestion": "Use environment variable: SECRET_KEY = os.environ.get('SECRET_KEY') and store the value in .env (gitignored)."},
        {"type": "bug", "severity": "high", "file": "app/utils.py", "line": 87,
         "title": "Unhandled None Return",
         "description": "get_user() can return None but the result is accessed without a null check, causing AttributeError at runtime.",
         "suggestion": "Add guard: user = get_user(id)\nif user is None: raise HTTPException(status_code=404, detail='Not found')"},
        {"type": "performance", "severity": "medium", "file": "app/views.py", "line": 112,
         "title": "N+1 Query in Loop",
         "description": "A database query runs inside a for-loop. With 1,000 records this executes 1,001 queries instead of 1.",
         "suggestion": "Batch fetch outside the loop: users = User.objects.filter(id__in=user_ids).select_related('profile')"},
        {"type": "style", "severity": "low", "file": "app/models.py", "line": 56,
         "title": "Magic Number Without Context",
         "description": "The literal 86400 appears with no explanation. Future developers won't know this is seconds-per-day.",
         "suggestion": "Use a named constant: SECONDS_PER_DAY = 86400  # or: from datetime import timedelta; timedelta(days=1).total_seconds()"},
    ]
    summary = ("Critical SQL injection and hardcoded secret key must be fixed before merge. "
               "An N+1 query pattern will cause performance degradation at scale. "
               "Minor style concerns exist but are non-blocking.")
    return issues, summary, 42, "demo"

# ── Pipeline ─────────────────────────────────────────────────────
async def run_pr_review(pr_key: str, owner: str, repo: str,
                         pr_number: int, commit_sha: str):
    pr_store[pr_key]["status"] = "analyzing"
    diff = ""
    try:
        diff, meta = await fetch_pr_diff(owner, repo, pr_number)
        pr_store[pr_key].update({
            "files_changed": meta.get("changed_files", 0),
            "additions": meta.get("additions", 0),
            "deletions": meta.get("deletions", 0),
        })
    except Exception as e:
        print(f"[GitHub] Could not fetch diff: {e}")

    # Try Groq first (fastest), then Gemini, then demo
    issues, summary, score, provider = [], "", 75, "demo"
    try:
        if GROQ_API_KEY:
            issues, summary, score, provider = await analyze_with_groq(
                diff or "(no diff available — demo mode)", pr_store[pr_key].get("title",""))
        elif GEMINI_API_KEY:
            issues, summary, score, provider = await analyze_with_gemini(
                diff or "(no diff available — demo mode)", pr_store[pr_key].get("title",""))
        else:
            issues, summary, score, provider = demo_analysis()
    except Exception as e:
        print(f"[AI] Analysis failed: {e}")
        issues, summary, score, provider = demo_analysis()

    pr_store[pr_key].update({
        "status": "complete",
        "issues": issues,
        "summary": summary,
        "score": score,
        "analyzed_at": datetime.utcnow().isoformat(),
        "ai_provider": provider,
    })

    # Update stats
    review_stats["total_prs"] += 1
    review_stats["total_issues"] += len(issues)
    for i in issues:
        review_stats["issues_by_severity"][i.get("severity","low")] = \
            review_stats["issues_by_severity"].get(i.get("severity","low"), 0) + 1
        review_stats["issues_by_type"][i.get("type","style")] = \
            review_stats["issues_by_type"].get(i.get("type","style"), 0) + 1

    # Post to GitHub
    if GITHUB_TOKEN:
        await post_github_review(owner, repo, pr_number, issues, summary, score, commit_sha)

# ── Routes ───────────────────────────────────────────────────────
@app.post("/webhook/github")
async def github_webhook(request: Request, background_tasks: BackgroundTasks):
    payload = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    if sig and not verify_github_signature(payload, sig):
        raise HTTPException(status_code=401, detail="Invalid signature")

    event = request.headers.get("X-GitHub-Event", "")
    data = json.loads(payload)

    if event != "pull_request":
        return {"status": "ignored", "event": event}
    if data.get("action") not in ("opened", "synchronize", "reopened"):
        return {"status": "ignored", "action": data.get("action")}

    pr = data["pull_request"]
    repo_full = data["repository"]["full_name"]
    owner, repo = repo_full.split("/")
    pr_number = pr["number"]
    pr_key = f"{repo_full}#{pr_number}"

    pr_store[pr_key] = {
        "pr_id": pr_key, "repo": repo_full, "title": pr["title"],
        "author": pr["user"]["login"], "url": pr["html_url"],
        "status": "pending", "issues": [], "summary": "", "score": 100,
        "analyzed_at": None, "ai_provider": "pending",
        "files_changed": pr.get("changed_files", 0),
        "additions": pr.get("additions", 0),
        "deletions": pr.get("deletions", 0),
    }
    background_tasks.add_task(run_pr_review, pr_key, owner, repo, pr_number, pr["head"]["sha"])
    return {"status": "queued", "pr": pr_key}

@app.post("/analyze/demo")
async def analyze_demo(background_tasks: BackgroundTasks):
    import random
    demos = [
        {"repo": "acme/backend-api", "num": 142, "title": "Add user authentication endpoints", "author": "alice"},
        {"repo": "acme/frontend",    "num": 87,  "title": "Refactor payment form validation",  "author": "bob"},
        {"repo": "acme/data-pipe",   "num": 33,  "title": "Optimize ETL job for large datasets","author": "charlie"},
    ]
    d = random.choice(demos)
    pr_key = f"{d['repo']}#{d['num']}-{int(time.time())}"
    pr_store[pr_key] = {
        "pr_id": pr_key, "repo": d["repo"], "title": d["title"],
        "author": d["author"], "url": f"https://github.com/{d['repo']}/pull/{d['num']}",
        "status": "pending", "issues": [], "summary": "", "score": 100,
        "analyzed_at": None, "ai_provider": "pending",
        "files_changed": random.randint(3, 12),
        "additions": random.randint(50, 400),
        "deletions": random.randint(10, 100),
    }
    background_tasks.add_task(run_pr_review, pr_key, "acme",
                               d["repo"].split("/")[1], d["num"], "abc1234")
    return {"status": "queued", "pr_key": pr_key}

@app.get("/prs")
async def list_prs():
    return list(pr_store.values())

@app.get("/prs/{pr_key:path}")
async def get_pr(pr_key: str):
    pr = pr_store.get(pr_key)
    if not pr:
        raise HTTPException(status_code=404, detail="PR not found")
    return pr

@app.get("/stats")
async def get_stats():
    return review_stats

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "groq_ready": bool(GROQ_API_KEY),
        "gemini_ready": bool(GEMINI_API_KEY),
        "github_ready": bool(GITHUB_TOKEN),
        "active_provider": "groq" if GROQ_API_KEY else ("gemini" if GEMINI_API_KEY else "demo"),
    }

@app.get("/")
async def serve_dashboard():
    """Serve the frontend dashboard — fix: explicit UTF-8 encoding prevents
    Windows 'charmap' codec crash on emoji characters in index.html."""
    frontend = Path(__file__).parent.parent / "frontend" / "index.html"
    if not frontend.exists():
        return HTMLResponse("<h1>Dashboard not found</h1><p>Make sure frontend/index.html exists.</p>", status_code=404)
    return HTMLResponse(content=frontend.read_text(encoding="utf-8"))
