"""
CodeSentinel — AI-Powered GitHub PR Reviewer (FastAPI Backend)
Uses FREE AI APIs: Groq (Llama 3.3 70B) + Google Gemini 2.0 Flash
Run: uvicorn main:app --reload
"""

import hmac
import hashlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

load_dotenv()

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="CodeSentinel — AI Code Review", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")
GITHUB_WEBHOOK_SECRET: str = os.getenv("GITHUB_WEBHOOK_SECRET", "")
GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")

# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------

pr_store: dict = {}
review_stats: dict = {
    "total_prs": 0,
    "total_issues": 0,
    "issues_by_severity": {"critical": 0, "high": 0, "medium": 0, "low": 0},
    "issues_by_type": {"bug": 0, "security": 0, "performance": 0, "style": 0},
}

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ReviewIssue(BaseModel):
    type: str
    severity: str
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


# ---------------------------------------------------------------------------
# HMAC verification
# ---------------------------------------------------------------------------


def verify_github_signature(payload: bytes, signature: str) -> bool:
    """Return True when the HMAC-SHA256 signature matches, or when no secret
    is configured (development mode)."""
    if not GITHUB_WEBHOOK_SECRET:
        return True
    mac = hmac.new(
        GITHUB_WEBHOOK_SECRET.encode(), msg=payload, digestmod=hashlib.sha256
    )
    expected = f"sha256={mac.hexdigest()}"
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


def build_review_prompt(diff: str, pr_title: str) -> str:
    """Return a strict JSON-only review prompt for any LLM."""
    return f"""You are a senior staff software engineer and security reviewer.

Analyze this GitHub Pull Request deeply and find ALL possible issues.

PR Title:
{pr_title}

Code Diff:
{diff[:15000]}

Review for these categories:

1. SECURITY VULNERABILITIES
   - Hardcoded secrets, API keys, passwords, tokens
   - SQL injection, command injection
   - Unsafe eval / exec usage
   - Sensitive data exposure
   - Authentication / authorisation flaws
   - Missing input validation
   - XSS, CSRF, path traversal
   - Insecure random, weak crypto
   - Webhook signature bypass

2. BUGS
   - Null / None dereference
   - Division by zero
   - Wrong logic or incorrect conditions
   - Uncaught exceptions / missing error handling
   - Infinite loops
   - Off-by-one errors
   - Race conditions

3. PERFORMANCE PROBLEMS
   - O(n²) or worse complexity
   - N+1 database queries
   - Memory leaks or large allocations
   - Repeated / redundant computation
   - Blocking I/O in async context
   - Unnecessary loops

4. CODE QUALITY / STYLE
   - Poor variable / function naming
   - Dead or unreachable code
   - Unused imports or variables
   - Magic numbers without constants
   - Overly long functions
   - Duplicate logic

5. MAINTAINABILITY
   - Missing docstrings / comments on complex logic
   - Tight coupling
   - Repeated patterns that should be extracted

Return STRICT JSON ONLY — no markdown, no prose, no code fences.

Respond with EXACTLY this structure:

{{
  "issues": [
    {{
      "type": "security|bug|performance|style",
      "severity": "critical|high|medium|low",
      "file": "path/to/file.py",
      "line": 42,
      "title": "Short issue title",
      "description": "Why this is a problem",
      "suggestion": "Concrete fix or improved code"
    }}
  ],
  "summary": "2-3 sentence overall assessment",
  "score": 78
}}

Rules:
- Only report issues visible in the provided diff.
- Do NOT invent file names, functions, or line numbers.
- Use ONLY filenames present in the diff header lines (e.g. +++ b/path/file.py).
- If there are no issues, return an empty issues array and score 100.
- Only return valid JSON.
"""


# ---------------------------------------------------------------------------
# AI response parser
# ---------------------------------------------------------------------------


def parse_ai_response(text: str) -> dict:
    """Safely parse the LLM JSON response.

    Never raises — always returns a valid dict with ``issues``, ``summary``,
    and ``score`` keys so the rest of the pipeline cannot crash.
    """
    fallback: dict = {"issues": [], "summary": "AI response could not be parsed.", "score": 75}

    if not text or not text.strip():
        return fallback

    # 1. Strip markdown code fences that some models add despite instructions.
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    cleaned = cleaned.rstrip("`").strip()

    # 2. Extract the outermost {...} block in case there is leading/trailing prose.
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        return fallback

    json_str = match.group(0)

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        # Last resort: try to salvage truncated JSON by appending closing brackets.
        for suffix in ("]}", "}}"):
            try:
                data = json.loads(json_str + suffix)
                break
            except json.JSONDecodeError:
                pass
        else:
            return fallback

    if not isinstance(data, dict):
        return fallback

    # Normalise issues list — tolerate both "type" and "category" keys.
    raw_issues = data.get("issues", [])
    if not isinstance(raw_issues, list):
        raw_issues = []

    valid_types = {"bug", "security", "performance", "style"}
    valid_severities = {"critical", "high", "medium", "low"}

    issues: list[dict] = []
    for item in raw_issues:
        if not isinstance(item, dict):
            continue
        issue_type = str(item.get("type") or item.get("category") or "style").lower()
        if issue_type not in valid_types:
            issue_type = "style"
        severity = str(item.get("severity", "low")).lower()
        if severity not in valid_severities:
            severity = "low"
        issues.append(
            {
                "type": issue_type,
                "severity": severity,
                "file": str(item.get("file", "unknown")),
                "line": int(item["line"]) if str(item.get("line", "")).isdigit() else None,
                "title": str(item.get("title", "Untitled issue")),
                "description": str(item.get("description", "")),
                "suggestion": str(item.get("suggestion", "")),
            }
        )

    raw_score = data.get("score", 75)
    try:
        score = max(0, min(100, int(raw_score)))
    except (TypeError, ValueError):
        score = 75

    return {
        "issues": issues,
        "summary": str(data.get("summary", fallback["summary"])),
        "score": score,
    }


# ---------------------------------------------------------------------------
# AI analysis — Groq (Llama 3.3 70B, free tier)
# ---------------------------------------------------------------------------


async def analyze_with_groq(diff: str, pr_title: str) -> tuple[list, str, int, str]:
    """Call Groq's OpenAI-compatible endpoint with Llama 3.3 70B.

    Free tier: https://console.groq.com — 6 000 req/day, 500 K tokens/min.
    """
    prompt = build_review_prompt(diff, pr_title)

    async with httpx.AsyncClient(timeout=60) as client:
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
        )
        resp.raise_for_status()
        raw: str = resp.json()["choices"][0]["message"]["content"]

    result = parse_ai_response(raw)
    return result["issues"], result["summary"], result["score"], "groq/llama-3.3-70b"


# ---------------------------------------------------------------------------
# AI analysis — Google Gemini 2.0 Flash (free tier)
# ---------------------------------------------------------------------------


async def analyze_with_gemini(diff: str, pr_title: str) -> tuple[list, str, int, str]:
    """Call the Gemini 2.0 Flash generateContent endpoint.

    Free tier: https://aistudio.google.com — 1 500 req/day, no credit card.
    """
    prompt = build_review_prompt(diff, pr_title)
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    )

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            url,
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.2, "maxOutputTokens": 4096},
            },
        )
        resp.raise_for_status()
        payload = resp.json()

    # Navigate the Gemini candidate structure safely.
    try:
        raw: str = payload["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        raw = ""

    result = parse_ai_response(raw)
    return result["issues"], result["summary"], result["score"], "google/gemini-2.0-flash"


# ---------------------------------------------------------------------------
# Demo fallback (no API key configured)
# ---------------------------------------------------------------------------


def demo_analysis() -> tuple[list, str, int, str]:
    """Return a realistic set of example issues for demonstration purposes."""
    issues = [
        {
            "type": "security",
            "severity": "critical",
            "file": "app/auth.py",
            "line": 34,
            "title": "SQL Injection Vulnerability",
            "description": (
                "User input is concatenated directly into the SQL query without "
                "sanitisation, allowing an attacker to manipulate the query."
            ),
            "suggestion": (
                "Use parameterised queries: "
                "cursor.execute('SELECT * FROM users WHERE id = %s', (user_id,))"
            ),
        },
        {
            "type": "security",
            "severity": "high",
            "file": "config/settings.py",
            "line": 12,
            "title": "Hardcoded Secret Key",
            "description": (
                "A secret key is hardcoded in source code and will be committed to "
                "version control, exposing it to all contributors."
            ),
            "suggestion": (
                "Use an environment variable: "
                "SECRET_KEY = os.environ.get('SECRET_KEY') "
                "and store the value in .env (gitignored)."
            ),
        },
        {
            "type": "bug",
            "severity": "high",
            "file": "app/utils.py",
            "line": 87,
            "title": "Unhandled None Return",
            "description": (
                "get_user() can return None but the result is accessed without a "
                "null check, causing AttributeError at runtime."
            ),
            "suggestion": (
                "Add guard: user = get_user(id)\n"
                "if user is None: raise HTTPException(status_code=404, detail='Not found')"
            ),
        },
        {
            "type": "performance",
            "severity": "medium",
            "file": "app/views.py",
            "line": 112,
            "title": "N+1 Query in Loop",
            "description": (
                "A database query runs inside a for-loop. "
                "With 1 000 records this executes 1 001 queries instead of 1."
            ),
            "suggestion": (
                "Batch fetch outside the loop: "
                "users = User.objects.filter(id__in=user_ids).select_related('profile')"
            ),
        },
        {
            "type": "style",
            "severity": "low",
            "file": "app/models.py",
            "line": 56,
            "title": "Magic Number Without Context",
            "description": (
                "The literal 86400 appears with no explanation. "
                "Future developers will not know this represents seconds-per-day."
            ),
            "suggestion": (
                "Use a named constant: SECONDS_PER_DAY = 86400  "
                "# or: from datetime import timedelta; timedelta(days=1).total_seconds()"
            ),
        },
    ]
    summary = (
        "Critical SQL injection and hardcoded secret key must be fixed before merge. "
        "An N+1 query pattern will cause performance degradation at scale. "
        "Minor style concerns exist but are non-blocking."
    )
    return issues, summary, 42, "demo"


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------


async def fetch_pr_diff(owner: str, repo: str, pr_number: int) -> tuple[str, dict]:
    """Return (unified_diff_text, pr_metadata_dict) for the given PR."""
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        meta_resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
            headers=headers,
        )
        meta_resp.raise_for_status()
        meta: dict = meta_resp.json()

        diff_resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
            headers={**headers, "Accept": "application/vnd.github.v3.diff"},
        )
        diff_resp.raise_for_status()
        diff: str = diff_resp.text

    return diff, meta


async def post_github_review(
    owner: str,
    repo: str,
    pr_number: int,
    issues: list,
    summary: str,
    score: int,
    commit_sha: str,
) -> None:
    """Post an AI review comment directly to the GitHub PR."""
    if not GITHUB_TOKEN:
        return

    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    severity_icons = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}
    type_icons = {"bug": "🐛", "security": "🔒", "performance": "⚡", "style": "💅"}

    inline_comments = [
        {
            "path": i["file"],
            "line": i["line"],
            "body": (
                f"{type_icons.get(i['type'], 'ℹ️')} "
                f"**[{i['severity'].upper()}]** **{i['title']}**\n\n"
                f"{i['description']}\n\n**💡 Suggestion:** {i['suggestion']}"
            ),
        }
        for i in issues
        if i.get("line")
    ][:10]

    issue_rows = "\n".join(
        f"| {type_icons.get(i['type'], 'ℹ️')} {i['type'].title()} "
        f"| {severity_icons.get(i['severity'], '')} {i['severity'].title()} "
        f"| `{i['file']}` | {i['title']} |"
        for i in issues
    ) or "| — | — | — | No issues found ✅ |"

    if score >= 85:
        verdict = "APPROVE"
    elif score < 60:
        verdict = "REQUEST_CHANGES"
    else:
        verdict = "COMMENT"

    body = (
        f"## 🤖 AI Code Review — Score: {score}/100\n\n"
        f"{summary}\n\n"
        f"### Issues ({len(issues)})\n\n"
        f"| Type | Severity | File | Issue |\n"
        f"|------|----------|------|-------|\n"
        f"{issue_rows}\n\n"
        f"*Powered by CodeSentinel (Groq Llama 3.3 70B / Google Gemini 2.0 Flash)*"
    )

    async with httpx.AsyncClient(timeout=30) as client:
        await client.post(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
            headers=headers,
            json={
                "commit_id": commit_sha,
                "body": body,
                "event": verdict,
                "comments": inline_comments,
            },
        )


# ---------------------------------------------------------------------------
# Review pipeline
# ---------------------------------------------------------------------------


async def run_pr_review(
    pr_key: str,
    owner: str,
    repo: str,
    pr_number: int,
    commit_sha: str,
) -> None:
    """Background task: fetch diff → AI analysis → store result → post to GitHub."""
    pr_store[pr_key]["status"] = "analyzing"
    diff = ""

    try:
        diff, meta = await fetch_pr_diff(owner, repo, pr_number)
        pr_store[pr_key].update(
            {
                "files_changed": meta.get("changed_files", 0),
                "additions": meta.get("additions", 0),
                "deletions": meta.get("deletions", 0),
            }
        )
    except Exception as exc:
        print(f"[GitHub] Could not fetch diff: {exc}")

    issues: list = []
    summary = ""
    score = 75
    provider = "demo"
    diff_text = diff or "(no diff available — demo mode)"
    pr_title = pr_store[pr_key].get("title", "")

    try:
        if GROQ_API_KEY:
            issues, summary, score, provider = await analyze_with_groq(diff_text, pr_title)
        elif GEMINI_API_KEY:
            issues, summary, score, provider = await analyze_with_gemini(diff_text, pr_title)
        else:
            issues, summary, score, provider = demo_analysis()
    except Exception as exc:
        print(f"[AI] Analysis failed ({provider}): {exc}")
        issues, summary, score, provider = demo_analysis()

    pr_store[pr_key].update(
        {
            "status": "complete",
            "issues": issues,
            "summary": summary,
            "score": score,
            "analyzed_at": datetime.utcnow().isoformat(),
            "ai_provider": provider,
        }
    )

    # Update aggregate statistics.
    review_stats["total_prs"] += 1
    review_stats["total_issues"] += len(issues)
    for issue in issues:
        sev = issue.get("severity", "low")
        typ = issue.get("type", "style")
        review_stats["issues_by_severity"][sev] = (
            review_stats["issues_by_severity"].get(sev, 0) + 1
        )
        review_stats["issues_by_type"][typ] = (
            review_stats["issues_by_type"].get(typ, 0) + 1
        )

    if GITHUB_TOKEN:
        try:
            await post_github_review(
                owner, repo, pr_number, issues, summary, score, commit_sha
            )
        except Exception as exc:
            print(f"[GitHub] Could not post review: {exc}")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/")
async def serve_dashboard() -> HTMLResponse:
    """Serve the frontend dashboard (frontend/index.html) if it exists."""
    frontend = Path(__file__).parent.parent / "frontend" / "index.html"
    if not frontend.exists():
        return HTMLResponse(
            "<h1>CodeSentinel</h1><p>Frontend not found. "
            "Make sure <code>frontend/index.html</code> exists.</p>",
            status_code=200,
        )
    return HTMLResponse(content=frontend.read_text(encoding="utf-8"))


@app.get("/health")
async def health() -> dict:
    """Liveness / readiness probe."""
    return {
        "status": "ok",
        "groq_ready": bool(GROQ_API_KEY),
        "gemini_ready": bool(GEMINI_API_KEY),
        "github_ready": bool(GITHUB_TOKEN),
        "active_provider": (
            "groq" if GROQ_API_KEY else ("gemini" if GEMINI_API_KEY else "demo")
        ),
    }


@app.get("/api/dashboard")
async def api_dashboard() -> dict:
    """Aggregate statistics used by the dashboard UI."""
    prs = list(pr_store.values())
    completed = [p for p in prs if p.get("status") == "complete"]
    avg_score = (
        round(sum(p.get("score", 0) for p in completed) / len(completed))
        if completed
        else 0
    )
    return {
        "stats": review_stats,
        "avg_score": avg_score,
        "recent_prs": sorted(
            prs, key=lambda p: p.get("analyzed_at") or "", reverse=True
        )[:10],
    }


@app.get("/api/pull-requests")
async def api_pull_requests() -> list:
    """Return all PR reviews sorted by most recently analysed."""
    return sorted(
        pr_store.values(),
        key=lambda p: p.get("analyzed_at") or "",
        reverse=True,
    )


@app.get("/prs")
async def list_prs() -> list:
    """Alias kept for backwards compatibility."""
    return list(pr_store.values())


@app.get("/prs/{pr_key:path}")
async def get_pr(pr_key: str) -> dict:
    pr = pr_store.get(pr_key)
    if not pr:
        raise HTTPException(status_code=404, detail="PR not found")
    return pr


@app.get("/stats")
async def get_stats() -> dict:
    return review_stats


@app.post("/webhook/github")
async def github_webhook(
    request: Request, background_tasks: BackgroundTasks
) -> dict:
    """Receive GitHub webhook events and trigger AI review for opened / synchronised PRs."""
    payload = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    if sig and not verify_github_signature(payload, sig):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    event = request.headers.get("X-GitHub-Event", "")
    try:
        data: dict = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Malformed JSON payload") from exc

    if event != "pull_request":
        return {"status": "ignored", "event": event}

    action = data.get("action", "")
    if action not in ("opened", "synchronize", "reopened"):
        return {"status": "ignored", "action": action}

    pr = data["pull_request"]
    repo_full: str = data["repository"]["full_name"]
    owner, repo = repo_full.split("/", 1)
    pr_number: int = pr["number"]
    pr_key = f"{repo_full}#{pr_number}"

    pr_store[pr_key] = {
        "pr_id": pr_key,
        "repo": repo_full,
        "title": pr["title"],
        "author": pr["user"]["login"],
        "url": pr["html_url"],
        "status": "pending",
        "issues": [],
        "summary": "",
        "score": 100,
        "analyzed_at": None,
        "ai_provider": "pending",
        "files_changed": pr.get("changed_files", 0),
        "additions": pr.get("additions", 0),
        "deletions": pr.get("deletions", 0),
    }

    background_tasks.add_task(
        run_pr_review, pr_key, owner, repo, pr_number, pr["head"]["sha"]
    )
    return {"status": "queued", "pr": pr_key}


@app.post("/analyze/demo")
async def analyze_demo(background_tasks: BackgroundTasks) -> dict:
    """Trigger a demo analysis without a real GitHub webhook."""
    import random

    demos = [
        {
            "repo": "acme/backend-api",
            "num": 142,
            "title": "Add user authentication endpoints",
            "author": "alice",
        },
        {
            "repo": "acme/frontend",
            "num": 87,
            "title": "Refactor payment form validation",
            "author": "bob",
        },
        {
            "repo": "acme/data-pipe",
            "num": 33,
            "title": "Optimise ETL job for large datasets",
            "author": "charlie",
        },
    ]
    d = random.choice(demos)
    pr_key = f"{d['repo']}#{d['num']}-{int(time.time())}"

    pr_store[pr_key] = {
        "pr_id": pr_key,
        "repo": d["repo"],
        "title": d["title"],
        "author": d["author"],
        "url": f"https://github.com/{d['repo']}/pull/{d['num']}",
        "status": "pending",
        "issues": [],
        "summary": "",
        "score": 100,
        "analyzed_at": None,
        "ai_provider": "pending",
        "files_changed": random.randint(3, 12),
        "additions": random.randint(50, 400),
        "deletions": random.randint(10, 100),
    }

    owner, repo_name = d["repo"].split("/", 1)
    background_tasks.add_task(
        run_pr_review, pr_key, owner, repo_name, d["num"], "abc1234"
    )
    return {"status": "queued", "pr_key": pr_key}
