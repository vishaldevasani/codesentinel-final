<<<<<<< HEAD
# 🛡️ CodeSentinel — AI Code Review Assistant
### 100% Free · No credit card · Groq Llama 3.3 70B or Google Gemini 2.0 Flash

Automatically reviews GitHub Pull Requests and posts inline AI comments the moment a PR is opened.

---

## ⚡ Quick Start (5 minutes)

### 1. Clone & configure
```bash
git clone https://github.com/YOUR_USERNAME/codesentinel.git
cd codesentinel/backend
pip install -r requirements.txt
cp .env.example .env
# Open .env and paste your free API key (see below)
```

### 2. Get a FREE Groq API key (2 min)
1. Go to **https://console.groq.com** — sign in with GitHub, no card needed
2. **API Keys → Create API Key** → copy the key (starts with `gsk_`)
3. Paste it in `.env` as `GROQ_API_KEY=gsk_...`

### 3. Run
```bash
uvicorn main:app --reload
# Dashboard → http://127.0.0.1:8000
```

### 4. Connect GitHub (optional, for real PR reviews)
```bash
# Expose your local server to the internet
ngrok http 8000
# Copy the https URL (e.g. https://abc123.ngrok.io)
```
In your GitHub repo: **Settings → Webhooks → Add webhook**
- Payload URL: `https://abc123.ngrok.io/webhook/github`
- Content type: `application/json`
- Secret: paste your `GITHUB_WEBHOOK_SECRET` from `.env`
- Events: ✅ **Pull requests**

---

## 🔄 How It Works — GitHub PR Flow

```
Developer opens / pushes a PR
        │
        ▼
GitHub sends POST /webhook/github
        │
        ├─▶ Verify HMAC signature (security)
        ├─▶ Fetch full PR diff from GitHub API
        ├─▶ Send diff to Groq Llama 3.3 70B (or Gemini fallback)
        ├─▶ Parse JSON response → issues list
        ├─▶ Store in-memory + update stats
        └─▶ POST inline review comments back to GitHub
                │
                ▼
        Dashboard at http://127.0.0.1:8000
        updates in real-time (polling /prs)
```

### What gets detected

| Type | Examples |
|------|---------|
| 🔒 Security | SQL injection, XSS, hardcoded secrets, path traversal, CSRF |
| 🐛 Bugs | Null dereferences, off-by-one errors, uncaught exceptions |
| ⚡ Performance | N+1 queries, O(n²) loops, unnecessary allocations |
| 💅 Style | Magic numbers, dead code, poor naming, missing error handling |

---

## 🚀 Pushing to GitHub — Step by Step

```bash
# First time setup
git init
git add .
git commit -m "feat: initial CodeSentinel setup"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/codesentinel.git
git push -u origin main

# Every subsequent change
git add backend/main.py          # or: git add .
git commit -m "fix: add UTF-8 encoding to serve_dashboard"
git push
```

### ⚠️ NEVER push your `.env` file
The `.gitignore` in this repo already excludes `.env`. Double-check with:
```bash
git status   # .env should NOT appear here
```

### CI/CD — Automatic checks on every push
This repo includes `.github/workflows/ci.yml`. On every `git push`, GitHub Actions will:
1. Install dependencies
2. Lint `main.py` for syntax errors
3. Verify the app can start without crashing
4. Hit `/health` to confirm all routes respond

---

## 🌐 API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Dashboard UI |
| POST | `/webhook/github` | GitHub webhook receiver |
| POST | `/analyze/demo` | Trigger a demo review |
| GET | `/prs` | List all reviewed PRs |
| GET | `/prs/{key}` | Single PR + issues |
| GET | `/stats` | Aggregate stats |
| GET | `/health` | Provider status |

---

## 🔑 Free AI Providers

| Provider | Model | Free Limit | Sign Up |
|----------|-------|-----------|---------|
| **Groq** (recommended) | Llama 3.3 70B | 6,000 req/day · 500K tok/min | https://console.groq.com |
| **Google Gemini** (fallback) | Gemini 2.0 Flash | 1,500 req/day | https://aistudio.google.com |

No API key? App runs in **demo mode** automatically — 5 pre-loaded realistic PRs.

---

## 🐛 Common Issues

| Error | Cause | Fix |
|-------|-------|-----|
| `UnicodeDecodeError: charmap` | `read_text()` used wrong encoding on Windows | Fixed: `read_text(encoding='utf-8')` |
| `500 Internal Server Error` on `/` | `serve_dashboard` route was missing | Fixed: route added to main.py |
| `ModuleNotFoundError: dotenv` | `python-dotenv` not installed | `pip install python-dotenv` |
| Port 8000 already in use | Another process on 8000 | `uvicorn main:app --port 8001` |
=======
# codesentinel-final
>>>>>>> eef207cd5a0776035d71254be529848d6121a32f
