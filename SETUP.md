# SonarFix — Setup Guide

This package contains the SonarFix source code only. No database, no repo
clones, no credentials are included — you provide your own.

## Prerequisites

Install these first, then confirm each with the version command shown:

| Tool | Check | Needed for |
|---|---|---|
| Python 3.11+ | `python --version` | Backend |
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | `uv --version` | Python dependency manager |
| Node.js 20+ | `node --version` | Frontend |
| Git | `git --version` | Cloning repos SonarFix fixes issues in |
| [Claude Code CLI](https://github.com/anthropics/claude-code) | `claude --version` | AI engine (only if using `claude-code` engine, see below) |

## 1. Backend setup

```powershell
cd sonarfix
uv sync --extra all
```

`--extra all` pulls in every optional integration (local LLM providers, the
Claude Agent SDK, the SonarQube MCP adapter). If you only need the default
setup (Claude via API, no MCP), `uv sync` alone is enough.

## 2. Configure environment

```powershell
copy .env.example .env
```

Edit `.env` and fill in:

- **`SONAR_URL`**, **`SONAR_TOKEN`** — your SonarQube/SonarCloud instance and a
  personal access token (`My Account > Security > Generate Token`)
- **`ANTHROPIC_API_KEY`** — if using the Anthropic provider (default)
- **`BITBUCKET_TOKEN`** — a repository/project access token, used to push fix
  branches and open pull requests
- **`BITBUCKET_USERNAME`** — only needed if your token requires basic auth
  (Bitbucket Cloud app passwords)

Every other field in `.env.example` has sensible defaults and is documented
inline — read the comments before changing them.

**Do not commit `.env`.** It's already gitignored. Never share a filled-in
`.env` file between people or teams — each person/environment should
generate and own their own tokens.

### AI engine choice

- `SONARFIX_ENGINE=deepagents` (default) — works with any LLM provider
  (Anthropic, Ollama, OpenAI-compatible).
- `SONARFIX_ENGINE=claude-code` — uses the Claude Agent SDK driving the
  Claude Code CLI directly. Requires `SONARFIX_LLM_PROVIDER=anthropic` and:
  ```powershell
  npm install -g @anthropic-ai/claude-code
  claude login
  ```

## 3. Frontend setup

```powershell
cd frontend
npm install
copy .env.example .env   # if present; otherwise create one — see below
```

Frontend `.env` just needs:
```
VITE_API_URL=http://127.0.0.1:8000
```
(Point this at wherever the backend actually runs, if not localhost.)

## 4. Run it

Two terminals, from the repo root:

```powershell
# Terminal 1 — backend
uv run sonarfix api

# Terminal 2 — frontend
cd frontend
npm run dev
```

The frontend dev server prints its URL (typically `http://localhost:5173`).
Open that in a browser. It talks to the backend over `VITE_API_URL`.

## 5. First run, in the browser

1. Go to **Projects**, click **Sync projects from SonarQube** — this pulls
   your real project list from the configured `SONAR_URL`.
2. Pick a project, click **Use path** and point it at the local clone of
   that project's repository on disk (SonarFix needs this to read code
   context and write fixes).
3. Open an issue, click **Analyze with AI**, review the plan, approve, build,
   then open the pull request.

## Known gotchas

- **Claude Code CLI version**: SonarFix needs a recent build. If analysis
  fails with a version-mismatch error, run `claude update`.
- **Corporate TLS interception**: if your network intercepts HTTPS (common on
  corporate VPNs), SonarFix uses the OS trust store automatically — no extra
  config needed, but if you see `CERTIFICATE_VERIFY_FAILED` errors, check your
  machine's root CA is actually installed in Windows' certificate store.
- **One project per repo clone**: for monorepos with multiple SonarQube
  projects pointing at the same git repo, configure the **composite**
  project's repo path too (see `README.md` for details on `COMPOSITE_PROJECTS`),
  not just the individual sub-project — otherwise issues under the
  sub-projects won't find the local code.
- **Build verification** supports .NET/Maven/Gradle/CMake/C++/Java projects
  out of the box; it auto-detects the build system from files in the repo.

## What's NOT in this package

- `data/` (SQLite database, local repo clones, backups) — generated fresh on
  first run at `data/sonarfix.db`
- `.env` — you create your own from `.env.example`
- `.venv`, `node_modules` — installed via `uv sync` / `npm install` above
- `.git` history

## Questions

See `README.md` and `Guide.md` in this package for deeper architecture notes.
