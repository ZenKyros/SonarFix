# SonarFix

AI-assisted remediation of SonarQube issues. SonarFix reads a project's issues from SonarQube, groups
them by rule, fixes what it can on a separate git branch, builds the result, and opens a pull request.
**A human approves every AI change, and SonarFix never merges anything.**

## How it works

1. **Sync** projects and issues from SonarQube. Pick which SonarQube branch to read when a project has several.
2. **Plan** - issues are grouped by rule. Each group is either
   - **mechanical**: a fixed recipe that changes the code directly and costs no tokens, or
   - **AI**: one AI session per group (not per issue), only after you approve it.
3. **Batch** - the approved groups run on one new branch in your local clone, one commit per group.
   A dirty working tree is stashed first (`git stash list` to find it).
4. **Build** - you click Build. SonarFix builds the changed projects and what depends on them
   (C#/.NET via MSBuild or `dotnet`, C++, Maven, Gradle, npm). Test results are shown but do not block.
5. **If the build fails** - an error list (like Visual Studio's), a verdict on whether the fix or the machine
   caused it, why it happened, how to prevent it, and recommended actions. An action marked *AI will do this*
   has an **Apply with AI** button and an optional box for extra instructions.
6. **Review locally** - the fix lives on its own branch. The batch page can switch your clone onto it so you
   can open it and build it in Visual Studio, and switch back afterwards.
7. **Pull request** - SonarFix pushes the branch and opens a PR on Bitbucket (Cloud, Server, Data Center) or
   Azure DevOps Repos, chosen from the clone's `origin` remote.

Also included: an **activity log** of everything the AI and SonarFix did for each batch (live while it runs),
a **dependency scanner** with upgrade analysis (vulnerable and outdated NuGet / npm / Maven / Gradle packages,
trial-upgraded in a scratch copy, so your tree is untouched), a dashboard, and downloadable HTML reports.

## Requirements

- Python 3.11+ and [uv](https://docs.astral.sh/uv/) (`pip install uv`)
- Node.js 18+ (frontend)
- Git on PATH
- A SonarQube (or SonarCloud) URL and a user token
- Claude access: the [Claude Code CLI](https://www.npmjs.com/package/@anthropic-ai/claude-code)
  (`npm install -g @anthropic-ai/claude-code`, then `claude auth login`), or an `ANTHROPIC_API_KEY`
- The build tools for the projects being fixed (Visual Studio / MSBuild or the .NET SDK, JDK + Maven, Node, ...)
- A local git clone of each project, **on the branch SonarQube analysed**

## Setup

```bash
uv sync --extra all
cd frontend && npm install && cd ..
cp .env.example .env      # then fill it in; see below
```

The database is created automatically on first start (`uv run sonarfix init-db` does it by hand).

## Configuration

Everything is in `.env` (see [.env.example](.env.example), which lists every setting). The essentials:

| Setting | Purpose |
|---|---|
| `SONAR_URL`, `SONAR_TOKEN`, `SONAR_ORG` | SonarQube access (`SONAR_ORG` for SonarCloud only) |
| `SONARFIX_ENGINE` | `claude-code` (your Claude login, no API key) or `deepagents` (needs `ANTHROPIC_API_KEY` or a local model) |
| `SONARFIX_MODEL` | Claude model; also switchable on the Settings page |
| `BITBUCKET_TOKEN`, `BITBUCKET_USERNAME` | Pull requests on Bitbucket |
| `AZURE_DEVOPS_TOKEN` | Pull requests on Azure DevOps (PAT with Code read & write) |
| `SONARFIX_BUILD_ENABLED`, `SONARFIX_BUILD_TIMEOUT` | Build verification on/off, seconds per command |
| `SONARFIX_MAX_AI_OCCURRENCES` | Caps the occurrences sent to one AI session |

Check the setup with `uv run sonarfix check` (add `--live` to make a real model call).

## Run

```bash
# terminal 1 - backend (http://127.0.0.1:8000, API docs at /docs)
uv run uvicorn sonarfix.api.main:app --port 8000

# terminal 2 - frontend (http://localhost:5173)
cd frontend && npm run dev
```

Open http://localhost:5173. The **Settings** page can store credentials, install and sign in to Claude Code,
and choose the model, so you can start with an empty `.env`.

### Packaged app (Windows)

```bash
cd frontend && VITE_API_URL="" npm run build && cd ..
uv run --extra all pyinstaller build/sonarfix.spec --noconfirm --distpath build/dist --workpath build/work
```

This produces a single `SonarFix.exe`. It serves the UI itself and opens the browser. Its settings and database
live in `%LOCALAPPDATA%\SonarFix`.

## Using it

1. **Projects** - sync from SonarQube, bind each project to its local clone (or auto-clone), open it.
2. **Issues / Plan** - choose the SonarQube branch, review the groups, tick the mechanical groups and approve the
   AI groups you want, optionally add notes for the AI, then run the batch.
3. **Batch** - watch the steps and the activity log. When it reaches *awaiting build*, click **Build**.
4. **Build passed** - review the diff and build log, then **Send PR**.
   **Build failed** - read the explanation, apply a recommendation (or retry with your own notes), or give up.
   Either way the branch is kept, so nothing is lost.

## Layout

| Path | What it is |
|---|---|
| `frontend/` | React UI (Vite) |
| `src/sonarfix/api/` | FastAPI routes |
| `src/sonarfix/core/` | Planning, recipes, batch runs, build verification, build-failure summaries, activity log, SonarQube / git / PR clients |
| `prompts/` | System prompts for the analysis and fix agents |
| `build/` | PyInstaller spec and launcher for `SonarFix.exe` |

## Limits

- Fixes are only as good as the recipes and the AI; review every diff.
- C# / .NET is the best-covered ecosystem; others rely mostly on AI sessions.
- Fixes land on a local clone, so the clone must match the branch SonarQube scanned, otherwise line numbers drift.
- A project with no matching buildable project, or no build tool on the machine, is reported as
  *not verified* - that is never treated as a pass.

## Troubleshooting

- **"Not a git repository"** - the bound folder has no `.git`; bind the real clone.
- **Build failed with missing libraries** - usually your machine, not the fix: connect to the VPN, build the
  solution once in Visual Studio, then retry. The failure view says which.
- **No issues returned** - check the token's access to the project, `SONAR_URL`, and the selected SonarQube branch.
- **Claude not found** - `npm install -g @anthropic-ai/claude-code`, then `claude auth login` (or use the Settings page).
- **Fix isn't visible in Visual Studio** - it is committed on its own branch; use *Review and build locally* on the
  batch page, or `git checkout <branch>`.

## License

Proprietary - Unisys internal use only.
