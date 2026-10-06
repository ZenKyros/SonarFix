# SonarFix — Team Onboarding

This doc covers: what SonarFix is, what was added recently, how to get it running
locally with the fewest surprises, and a ready-to-paste prompt for Claude Code so
a new teammate (or a fresh Claude session) gets productive fast.

---

## 1. What SonarFix is

An AI-assisted remediation tool for SonarQube findings in .NET repos:

```
SonarQube issue -> AI analysis (root cause, plan) -> human approval
  -> AI/mechanical fix -> local build verification -> Bitbucket PR (human review)
```

Nothing is ever auto-merged. A human approves the plan before any code changes,
and the fix never reaches a PR unless it actually compiles.

### Recently added (this round of work)

- **Build verification** (`src/sonarfix/core/build.py`): after a fix is applied,
  it builds the affected project(s) for real before offering a PR. Auto-detects
  `dotnet build` (SDK-style `.csproj`) vs. full MSBuild (legacy `ToolsVersion`
  projects, found via `vswhere`). A failed build auto-discards the branch.
- **Single-issue PR creation**: `POST /issues/{id}/pull-request` pushes the
  branch and opens a real Bitbucket PR — only reachable once a run's `status`
  is `applied`, which itself only happens after a passing build.
- **One-step project onboarding**: `POST /onboard` — paste a SonarQube project
  link and a repo link (+ optional branch / local folder), get matching issues
  fetched automatically. It:
  - normalizes a Bitbucket *browse* URL (`.../projects/X/repos/y/browse`) into
    a real clone URL (`.../scm/X/y.git`) — browse URLs fail `git clone` with a
    confusing "auth failed" error otherwise.
  - lets you point at an already-cloned folder (binds it, skips re-cloning).
  - **rejects Portfolio/Application SonarQube links** — these aggregate issues
    across many unrelated repos and will silently return thousands of issues
    whose file paths don't exist in the one repo you're fixing. Use the
    **Project** link (`/code?id=...` or `/dashboard?id=...`), not
    `/portfolio?id=...` or `/application?id=...`.
- **UI**: build status banners + build log viewer, "Generate pull request"
  button, a single onboarding panel on the Projects page, Unisys-branded theme.

---

## 2. Prerequisites

- **Python 3.11+**, `uv` (`pip install uv`)
- **Node.js 18+** (frontend)
- **Git** on PATH
- **For building C# fixes locally:**
  - `dotnet` CLI — builds modern SDK-style projects
  - **Visual Studio Build Tools (MSBuild)** — required for legacy .NET Framework
    projects (old `ToolsVersion`-based `.csproj`). Found automatically via
    `vswhere` if installed under the default VS path.
  - The right **.NET Framework targeting packs** for whatever versions the
    target repo uses (check each `.csproj`'s `<TargetFrameworkVersion>`).
    Missing one causes `MSB3644` — a real, not-SonarFix-related, build failure.
  - NuGet packages **restored** for the target repo (`nuget restore` /
    `msbuild /t:restore`), or you'll see `MSB... missing NuGet package` errors
    that again have nothing to do with the fix itself.
- **SonarQube** instance + token (My Account → Security → Generate Token)
- **Bitbucket** (Server/DC or Cloud) personal access token

---

## 3. Environment setup

```bash
git clone <this repo>
cd Sonarfix
uv sync
cd frontend && npm install && cd ..
cp .env.example .env   # then fill in the values below
```

`.env` — required:

```env
SONAR_URL=https://your-sonarqube-host/
SONAR_TOKEN=sqp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

SONARFIX_ENGINE=claude-code
SONARFIX_MODEL=claude-haiku-4-5-20251001

BITBUCKET_TOKEN=your-bitbucket-token
BITBUCKET_USERNAME=your-username   # only needed for app-password/basic auth

SONARFIX_DB=data/sonarfix.db
SONARFIX_API_URL=http://127.0.0.1:8000
SONARFIX_CONTEXT_RADIUS=40
```

Optional, relevant to build verification:

```env
SONARFIX_BUILD_ENABLED=true     # set false to skip local build checks entirely
SONARFIX_BUILD_TIMEOUT=300      # seconds, per project built
```

`.env` is gitignored — never commit it. It holds real tokens.

---

## 4. Running it

**Backend** (two things to get right, or the import fails):

```bash
# PYTHONPATH must include src/ — the package is not pip-installed, the venv
# has no editable install, so plain `uv run uvicorn ...` can fail with
# "ModuleNotFoundError: No module named 'sonarfix'" unless uv resolves it.
# If that happens, set it explicitly:
#   PowerShell:  $env:PYTHONPATH = "$PWD\src"
#   bash:        export PYTHONPATH="$PWD/src"

uv run uvicorn sonarfix.api.main:app --reload --port 8000
```

```bash
cd frontend
npm run dev
```

Open `http://localhost:5173`. Check `http://127.0.0.1:8000/health` — it
reports which AI engine/model is wired up.

---

## 5. The fastest path to a working fix (what to expect)

1. **Projects page** → "Start a new project" panel → paste the SonarQube
   **Project** URL and the repo URL → Fetch issues.
2. Pick an issue → **Analyze with AI** → review the plan → **Approve**.
3. Watch the status: `applying` → `build_passed`/`failed` → `applied`.
   - If it fails and the error mentions `MSB3644` or "missing NuGet package",
     that's a local toolchain gap on your machine, not a bad fix — see
     Prerequisites above.
4. On `applied`, click **Generate pull request on Bitbucket**.

---

## 6. Gotchas we actually hit (so you don't have to)

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: No module named 'sonarfix'` | venv has no editable install | Set `PYTHONPATH` to `src/` before running uvicorn |
| `git: command not found` in a fresh shell | Git installed per-user, not on system PATH | `repo.py` auto-detects common install paths; if it still fails, add Git's `cmd/` folder to PATH |
| Build fails with `MSB3644` (reference assemblies not found) | Missing .NET Framework targeting pack for that project's version | Install the Developer Pack for that exact `TargetFrameworkVersion` |
| Build fails with "missing NuGet package(s)" | Repo was never `nuget restore`d on this machine | Run restore before relying on build verification for that repo |
| `git clone` "Authentication failed" against a URL that works fine in a browser | You pasted a Bitbucket **browse** page URL, not a clone URL | Already handled — `onboard`/`clone` normalize it automatically now |
| Onboarding returns thousands of issues with file paths that don't exist in your repo | You pasted a SonarQube **Portfolio/Application** link, not a **Project** link | Use the Project URL (`/code?id=...`); Portfolio links are now rejected with a clear error |
| `POST /onboard` says a folder "exists but is not a git repository" | The folder existed but was non-empty and not yet cloned into | Point at an empty or non-existent folder to clone into, or the actual clone root if it already has `.git` |
| Huge, unrelated file diffs show up in `git status` (cloned repos, MCP cache jars, SQLite WAL files) | `.gitignore` was missing entries for `data/repos/`, `.sonarfix/`, `*.db-wal`/`*.db-shm` | Already fixed in `.gitignore` — never commit those paths; they can contain proprietary source from whatever repo you pointed SonarFix at |

---

## 7. Prompt to paste into a new Claude Code session

Use this to get a fresh Claude session (or a teammate) oriented instantly,
without re-deriving architecture or repeating mistakes already fixed:

```
This is SonarFix: an AI-assisted SonarQube remediation tool for .NET repos.
Read ONBOARDING.md at the repo root first — it has setup steps, the current
architecture, and a table of real gotchas already hit and fixed (PYTHONPATH
for running uvicorn, Git PATH, MSBuild vs dotnet build selection, legacy
.NET Framework targeting packs, NuGet restore, Bitbucket browse-URL vs
clone-URL normalization, SonarQube Portfolio-vs-Project links, and what
.gitignore excludes and why).

Architecture: FastAPI backend (src/sonarfix/api/main.py) is a thin layer
over src/sonarfix/core/service.py, which owns all use-case logic. The
single-issue fix workflow is a LangGraph state machine in
src/sonarfix/core/graph.py: load_context -> analyze -> await_approval
(interrupts for human approval) -> apply_fix -> verify_build -> finalize.
verify_build (src/sonarfix/core/build.py) builds the fix's blast radius
before any PR is allowed; a failed build auto-discards the branch.
Batch (multi-issue) fixes go through src/sonarfix/core/batch.py instead,
which does NOT yet have build verification wired in - ask before assuming
it does.

Before touching git: this repo previously had cloned proprietary customer
source code (data/repos/*) and MCP cache binaries (.sonarfix/*) staged by
accident. Both are now gitignored. Never git add -A blindly here - check
git status --short and compare against .gitignore intent first.

Frontend is a Vite + React app under frontend/, plain CSS (no framework),
Unisys-branded (navy header #0a1f44, accent blue #0047bb - see
frontend/src/index.css :root).

Tell me what you'd like to work on.
```

---

## 8. Where things live (quick reference)

| Concern | File |
|---|---|
| FastAPI routes | `src/sonarfix/api/main.py` |
| Use-case logic (API and UI both go through this) | `src/sonarfix/core/service.py` |
| Single-issue fix workflow (LangGraph) | `src/sonarfix/core/graph.py` |
| Build verification | `src/sonarfix/core/build.py` |
| Batch (multi-issue) fix workflow | `src/sonarfix/core/batch.py` |
| SonarQube client + URL parsing | `src/sonarfix/core/sonar.py` |
| Bitbucket clone/push/PR + URL normalization | `src/sonarfix/core/scm.py` |
| Local git operations | `src/sonarfix/core/repo.py` |
| SQLite persistence | `src/sonarfix/core/store.py` |
| Config / env vars | `src/sonarfix/core/config.py` |
| Frontend pages | `frontend/src/pages/` |
| Frontend API client | `frontend/src/api.js` |
| Styling / theme | `frontend/src/index.css`, `frontend/src/flow.css` |
