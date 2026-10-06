# SonarFix — AI-Assisted Code Remediation

 SonarFix is an autonomous code remediation system that analyzes SonarQube findings, generates AI-powered fixes, and creates pull requests. It's designed for large .NET monorepos with cost optimization and human-in-the-loop approval gates.

**Key features:**
- 🔍 Issues clustered by pattern (one AI analysis per group, not per issue)
- 🤖 Live AI thinking display in the UI (progressive text reveal)
- ✅ Mechanical fixes run instantly (zero tokens)
- 🔐 Human approval required before any code changes
- 📊 Dependency graph for accurate blast radius estimation
- 🚀 Built for 144+ solutions, 861+ projects, legacy MSBuild monorepos

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Installation](#installation)
3. [Configuration](#configuration)
4. [Demo Setup](#demo-setup)
5. [Running the System](#running-the-system)
6. [Using the UI](#using-the-ui)
7. [Architecture](#architecture)
8. [Production Deployment](#production-deployment)
9. [Troubleshooting](#troubleshooting)

---

## Prerequisites

### System Requirements
- **Python 3.11+** (with `pip` and `venv`)
- **Node.js 18+** (for React frontend)
- **Git** on PATH
- **uv** package manager: `pip install uv`
- **MSBuild** (for legacy .NET projects) — comes with Visual Studio
- **dotnet CLI** (for SDK-style projects)

### Accounts & Services
- **SonarQube local instance** (http://localhost:9000 or your server URL)
  - Project key and token from **My Account > Security > Generate Token**
- **Bitbucket Server** (your repository URL and personal access token)
- **Claude AI access** via Claude Code SDK (OAuth, no API key needed)

### Repository
- A local **git clone** of your C# repository
- **Clean working tree** (no uncommitted changes — SonarFix will commit on your behalf)
- **Branch matching the SonarQube scan** (usually `main` or `develop`)

---

## Installation

### Step 1: Clone SonarFix

```bash
git clone https://github.com/your-org/sonarfix.git
cd sonarfix
```

### Step 2: Install Dependencies

```bash
# Python backend
uv sync

# React frontend
cd frontend
npm install
cd ..
```

### Step 3: Initialize the Database

```bash
uv run sonarfix init-db
```

This creates `data/sonarfix.db` (SQLite) with tables for projects, issues, fix plans, and runs.

---

## Configuration

### Step 1: Environment Variables

Copy the template and fill in your details:

```bash
cp .env.example .env
```

**Required:**

```env
# SonarQube
SONAR_URL=http://localhost:9000/
SONAR_TOKEN=sqp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
SONAR_ORG=                           # Leave blank for self-hosted; required for SonarCloud

# Claude (OAuth via Claude Code SDK — no API key)
SONARFIX_ENGINE=claude-code
SONARFIX_MODEL=claude-haiku-4-5-20251001    # Use Haiku for cost efficiency

# Bitbucket
BITBUCKET_TOKEN=BBDC-xxxxx
BITBUCKET_USERNAME=your-username

# Local paths
SONARFIX_DB=data/sonarfix.db
SONARFIX_API_URL=http://127.0.0.1:8000
SONARFIX_CONTEXT_RADIUS=40
```

**Optional:**

```env
# To use a cloud SonarQube instead of localhost
SONAR_URL=https://sonarcloud.io/

# Logging
SONARFIX_LOG_LEVEL=INFO
```

### Step 2: Verify Configuration

```bash
uv run sonarfix check
```

This verifies:
- ✅ SonarQube connectivity
- ✅ Git is available
- ✅ Database is initialized
- ✅ Claude Code SDK is configured

---

## Demo Setup

### Quick Start with DomainUpdateService

Use the included demo script to pull a real C# project and scan it:

```bash
# Copies source from the scanned tree, initializes git, imports issues
uv run python scripts/demo_setup.py

# Optional: pre-warm AI analysis for the top 3 issues (takes 5-10 min)
uv run python scripts/demo_setup.py --prewarm
```

This creates:
- `demo/DomainUpdateService/` — git repository
- 191 real SonarQube findings in the database
- Issue summary (14 mechanical, 177 AI-fixable in 30 groups)

Output:
```
[OK]   copied 21 file(s) from the scanned tree
[OK]   git repository ready on 'main'
[OK]   imported 191 issue(s) from SonarQube
[OK]   every issue resolves to a file in the repo

  191 issue(s) in 31 pattern(s)
      14  deterministic recipe   (no model call)
     177  need analysis          (30 group(s))
       0  skipped
```

---

## Running the System

### Terminal 1: Start the FastAPI Backend

```bash
cd /path/to/sonarfix
uv run uvicorn sonarfix.api.main:app --reload --port 8000
```

You should see:
```
INFO:     Uvicorn running on http://127.0.0.1:8000
INFO:     OpenAPI docs available at http://127.0.0.1:8000/docs
```

### Terminal 2: Start the React Frontend

```bash
cd /path/to/sonarfix/frontend
npm run dev
```

You should see:
```
Local:   http://localhost:5173/
```

### Terminal 3: Open the Browser

Navigate to **http://localhost:5173** and you're ready to go!

---

## Using the UI

### Screen 1: Projects

**What you see:**
- List of all SonarQube projects
- Issue counts per project (by severity)
- "Sync from SonarQube" button

**What to do:**
1. Click **"Sync from SonarQube"** to fetch the latest issues
2. Click a project to view its issues

### Screen 2: Issues List

**What you see:**
- All issues for the selected project
- Filters: Severity (BLOCKER / CRITICAL / MAJOR / MINOR / INFO)
- Type (CODE_SMELL / BUG / VULNERABILITY)
- Download HTML report button

**What to do:**
1. (Optional) Filter by severity or type
2. Click an issue to see details

### Screen 3: Issue Detail & Analysis

**What you see:**
1. Issue title, rule, severity, file path, and line number
2. Rule description (click to expand)
3. Code context (surrounding lines)
4. "Analyze with AI" button

**When you click "Analyze with AI":**
1. A spinning robot emoji appears
2. You see Claude's live thinking process (progressive text reveal, 3 chars every 10ms)
3. Confidence score appears when complete
4. Root cause, impact, and remediation plan show below

**Example thinking display:**
```
🤖 Claude is analyzing…

This is a constructor calling an overridable method 'OnStop', which violates the 
virtual method call anti-pattern. When a subclass overrides 'OnStop', its code 
runs during the parent's initialization, before fields are set up, causing null…
▌
```

**When you click "Approve and generate fix":**
1. Backend creates a feature branch: `sonarfix/batch-N` or `sonarfix/issue-ID`
2. Code is modified (either mechanical recipe or AI-generated)
3. Diff is shown
4. Commit message and PR description are generated
5. Build verification runs (rebuilds only the blast radius)
6. PR is opened on Bitbucket Server (for human review, never auto-merged)

### Download HTML Report

Click **"Download HTML report"** to export a summary of all issues, fixes applied, and status.

---

## Architecture

### System Diagram

```
┌─────────────────────────────────────────────────────────┐
│                    React UI (http://localhost:5173)     │
│         - Issue list, filters, analyze button          │
│         - Live thinking display                        │
│         - Approve/reject UI                            │
└──────────────────┬──────────────────────────────────────┘
                   │ HTTP
                   ▼
┌─────────────────────────────────────────────────────────┐
│         FastAPI Backend (http://127.0.0.1:8000)         │
│  - Project management (/projects/sync)                 │
│  - Issue fetching (/projects/{key}/sync-issues)        │
│  - Analysis orchestration (/issues/{id}/analyze)       │
│  - Fix application (/issues/{id}/approve)              │
└──────────────────┬──────────────────────────────────────┘
                   │
        ┌──────────┼──────────┐
        ▼          ▼          ▼
   ┌────────┐ ┌────────┐ ┌─────────┐
   │ SQLite │ │ SonarQube │ │ Claude  │
   │  DB    │ │   API  │ │ Code SDK│
   │        │ │        │ │ (OAuth) │
   └────────┘ └────────┘ └─────────┘
        │          │          │
   Issues,    Fetch rules,  AI analysis
   plans      find issues   & fix gen
```

### Component Breakdown

| Component | Purpose | Language |
|-----------|---------|----------|
| **frontend/** | React UI with live thinking | TypeScript/React |
| **src/sonarfix/api/** | FastAPI routes | Python |
| **src/sonarfix/core/** | Business logic (recipes, clustering, planning) | Python |
| **agents/** (optional) | PowerShell discovery agents for monorepo scanning | PowerShell |
| **data/sonarfix.db** | SQLite database (issues, fix plans, runs) | SQLite |

### Workflow Steps

```
1. SYNC
   ├─ Fetch projects from SonarQube
   ├─ Fetch issues for each project
   └─ Store in database

2. ANALYZE
   ├─ Group issues by (rule_key, normalized_code_hash)
   ├─ Show live AI thinking (progressive text reveal)
   ├─ Generate root cause, impact, remediation plan
   └─ Store plan in database (awaiting_approval = true)

3. APPROVE/REJECT
   ├─ Human reviews the plan
   └─ If approved: continue to APPLY

4. APPLY
   ├─ Create feature branch (sonarfix/batch-N)
   ├─ Route to recipe OR AI fix generator
   ├─ Commit changes with message
   ├─ Run build verification (blast radius only)
   └─ Open PR on Bitbucket Server

5. REVIEW & MERGE
   ├─ Human reviews the PR
   ├─ Approves or requests changes
   └─ Merges manually (SonarFix never merges)
```

---

## Production Deployment

### Step 1: Point to Your Real Repository

Instead of the demo, use your own C# monorepo:

```bash
uv run python scripts/setup_repo.py \
  --source "C:\path\to\your\monorepo" \
  --project "YOUR_SONAR_PROJECT_KEY" \
  --dest "C:\sonarfix\working\copy"
```

Or manually:
```bash
cp -r /path/to/your/repo /sonarfix/repo-copy
cd /sonarfix/repo-copy && git checkout main && git status  # Must be clean!
```

### Step 2: Sync Issues from SonarQube

```bash
# Via API
curl -X POST http://127.0.0.1:8000/projects/YOUR_KEY/sync-issues

# Or via UI: click "Sync from SonarQube"
```

### Step 3: Run Analysis in Batches

**Do NOT enable all issues at once** — AI analysis costs real tokens.

Instead:
1. Filter by **BLOCKER** severity (smallest batch)
2. Approve and fix a few
3. Monitor cost & quality
4. Expand to **CRITICAL** and **MAJOR**

**Cost estimate (with Haiku):**
- Per issue: $0.01–$0.02
- Per group (clustered): $0.05–$0.10
- For 191 issues in 30 groups: ~$2–$5 total

### Step 4: Configure Automatic Deployment (Optional)

Set up a **Bitbucket webhook** to trigger SonarFix when new scans arrive:

```python
# webhook handler (pseudocode)
@app.post("/webhook/sonarqube")
async def on_sonarqube_scan(payload):
    project_key = payload["project"]["key"]
    await service.sync_issues(project_key)
    # Optionally: analyze top N issues automatically
    return {"status": "synced"}
```

---

## Troubleshooting

### Issue: "Not a git repository"

**Cause:** Repository doesn't have a `.git` folder or was corrupted.

**Fix:**
```bash
cd /path/to/repo
git init
git add -A
git commit -m "Initial commit"
```

### Issue: "Build verification failed"

**Cause:** The fix broke the build.

**Solution:**
1. Check the error in the UI
2. Reject the plan
3. Re-analyze (AI will see the error message)
4. Approve the revised fix

### Issue: "SonarQube returned no issues"

**Cause:**
- Wrong project key
- Issues are resolved/closed in SonarQube
- Token lacks permissions

**Fix:**
1. Verify SONAR_TOKEN has access to the project
2. Check SONAR_URL (localhost vs SonarCloud)
3. Run `sonarqube:sonarqube scan` manually to force a new scan

### Issue: "Claude Code SDK not found"

**Cause:** CLI not installed.

**Fix:**
```bash
npm install -g @anthropic-ai/claude-code
claude auth login  # OAuth flow
```

### Issue: Analysis is very slow (5+ min per issue)

**Cause:** Using Opus model instead of Haiku.

**Fix:**
Edit `.env`:
```env
SONARFIX_MODEL=claude-haiku-4-5-20251001
```

Restart the backend and retry.

### Issue: "FOREIGN KEY constraint failed"

**Cause:** Database has orphaned fix_plans referencing deleted issues.

**Fix:**
```bash
rm data/sonarfix.db
uv run sonarfix init-db
```

(Your issues will re-sync from SonarQube.)

---

## Advanced: Model Switching

### Use Opus for Quality-Critical Fixes

Some rules require deeper analysis. Switch to **Claude Opus 5** for those:

```env
# .env
SONARFIX_MODEL=claude-opus-5    # For complex fixes
```

**Cost vs. Speed:**
| Model | Cost per issue | Speed | Best for |
|-------|---|---|---|
| Haiku | $0.01–$0.02 | 30–60s | High-volume demos, straightforward fixes |
| Opus | $0.10–$0.20 | 3–5m | Critical security fixes, architecture changes |

### Use Local LLM (Ollama)

For offline analysis without costs:

```bash
# Install Ollama: https://ollama.ai
ollama pull qwen2.5-coder:14b

# .env
SONARFIX_ENGINE=ollama
SONARFIX_MODEL=qwen2.5-coder:14b
SONARFIX_LLM_BASE_URL=http://localhost:11434
```

---

## What SonarFix Can & Cannot Do

### ✅ Can Do
- Fix **straightforward code issues** (unused imports, commented code, rethrows)
- Explain **why an issue matters** (root cause, impact)
- Generate **PR descriptions** with remediation details
- Handle **monorepos** (144+ solutions, 861+ projects)
- **Batch analyze** similar issues (30 groups from 191 issues)
- Create branches, commits, and PRs (never merge)

### ❌ Cannot Do
- Fix **architecture/design issues** (needs refactoring)
- Support **non-C# projects** (Java, Python, JS need recipe definitions)
- **Auto-merge PRs** (human review is required)
- Fix issues **without code context** (file-wide issues fail)
- Handle **build failures** (gates at verification step)

---

## Support & Feedback

- **Questions?** Check the [troubleshooting](#troubleshooting) section
- **Found a bug?** Open an issue on GitHub
- **Want a feature?** Describe it in a GitHub discussion

---

## License

Proprietary — Unisys Internal Use Only

---

**Last updated:** September 2026  
**Version:** 1.0  
**Model:** Claude Haiku 4.5 (default)
