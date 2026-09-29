# SonarFix

AI-assisted remediation of SonarQube issues. Pick a Sonar issue, let the agent
read the repository and explain it, approve the plan, and get a committed fix
with a commit message and PR description.

Nothing is written to your repository until you approve a plan, and nothing is
pushed or merged — ever. SonarFix commits to a local branch and hands you the
`git push` command.

## How it works

```
Streamlit UI  ──HTTP──▶  FastAPI  ──▶  LangGraph workflow (SQLite checkpointer)
                                         │
             load_context ──▶ analyze ──▶ await_approval ──┬──▶ apply_fix ──▶ finalize
             (Sonar + git)   (read-only   (interrupt:      │    (read-write   (commit msg,
                              agent)       human decides)  │     agent)        PR text, commit)
                                                           └──▶ rejected, stop
```

`await_approval` calls LangGraph's `interrupt()`, so a run parks durably in the
SQLite checkpointer. That is what lets the HTTP API stay request/response and
the Streamlit page re-render freely without losing a run in progress.

Fetching from Sonar, persistence and the PR text are plain functions. No agent
where a function does.

## Setup

Requires Python 3.11+, `git` on PATH, and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env     # then fill it in
uv run sonarfix init-db
uv run sonarfix check    # verifies Sonar, the model, the engine and MCP
uv run sonarfix check --live   # also calls the model and connects to MCP
```

`SONAR_URL` and `SONAR_TOKEN` are always required. What else you need depends
on the provider and engine you choose below.

## Running

Two processes, two terminals:

```bash
uv run sonarfix api     # http://127.0.0.1:8000  (docs at /docs)
uv run sonarfix ui      # http://localhost:8501
```

## Using it

1. **Projects** — sync from SonarQube, pick a project, and give it the path to a
   local git clone. The clone should be on the branch Sonar analysed, and its
   working tree must be clean (SonarFix commits for you, so it refuses to start
   on a dirty tree).
2. **Issues** — fetch issues, filter by severity and type, open one.
3. **Issue detail** — read the rule and the offending code, click **Analyze with
   AI**, read the plan and confidence score, then **Approve** (optionally with
   reviewer notes that override the plan) or **Reject**. On approval you get the
   diff, the commit message and the PR description, committed to a
   `sonarfix/<rule>-<issue>` branch.

Push it yourself when you're happy: `git push -u origin <branch>`.

---

## Choosing a model provider

`SONARFIX_LLM_PROVIDER` picks the backend. Everything else is unchanged — the
agents, the workflow and the UI do not know the difference.

| Provider | For | Needs |
|---|---|---|
| `anthropic` *(default)* | Claude | `ANTHROPIC_API_KEY` |
| `ollama` | Ollama on your machine | `uv sync --extra local`, a pulled model |
| `openai-compatible` | vLLM, LM Studio, llama.cpp, LiteLLM, any OpenAI-shaped endpoint | `uv sync --extra local`, `SONARFIX_LLM_BASE_URL` |

```bash
# Local via Ollama
uv sync --extra local
ollama pull qwen2.5-coder:14b
# in .env:
#   SONARFIX_LLM_PROVIDER=ollama
#   SONARFIX_MODEL=qwen2.5-coder:14b
uv run sonarfix check --live
```

**Read this before switching to a local model.** Both agents depend on reliable
tool calling and JSON-schema output. Frontier models do this well; small local
models often do not, and the failure mode is unhelpful — an agent that never
calls a tool, or returns output that will not parse. Pick a model advertised as
tool-calling capable, give it a large context window, and confirm with
`sonarfix check --live` before trusting a run.

## Choosing an agent engine

`SONARFIX_ENGINE` picks which agent implementation runs the analyse and fix
steps. Both produce the same structured output, so the rest of the workflow is
identical.

| Engine | What it is | Works with |
|---|---|---|
| `deepagents` *(default)* | LangGraph + [deepagents](https://docs.langchain.com/oss/python/deepagents), sandboxed to the repo by a virtual filesystem | every provider |
| `claude-code` | The [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk) — Claude Code as a library, with its own built-in tools and context management | `anthropic` only |

```bash
uv sync --extra claude-code
npm install -g @anthropic-ai/claude-code   # the SDK spawns this CLI
# in .env:  SONARFIX_ENGINE=claude-code
```

The `claude-code` engine runs headless, so `SONARFIX_CLAUDE_CODE_PERMISSION_MODE`
must be a mode that does not wait for a human at a terminal (`acceptEdits` by
default). It does not load your global `CLAUDE.md` or user settings — the files
in `prompts/` are the whole instruction set.

Tool access per engine:

| | analyse | fix |
|---|---|---|
| `deepagents` | `ls`, `read_file`, `glob`, `grep`; writes **denied** by permission rule | the above plus `edit_file`, `write_file` |
| `claude-code` | `Read`, `Grep`, `Glob` | the above plus `Edit`, `Write` |

Neither engine gets shell access, network tools, or anything outside the
repository directory.

## SonarQube MCP server (optional)

With `SONARFIX_SONAR_MCP` set, the agents also get SonarQube itself as tools —
so the analyst can ask follow-up questions ("what else does this rule flag in
this project?") instead of working only from the one issue we pre-fetched.

SonarFix does not bundle or manage that server; you point it at one. The same
configuration feeds both engines, and your `SONAR_URL` / `SONAR_TOKEN` /
`SONAR_ORG` are forwarded to it automatically.

```bash
uv sync --extra mcp        # only needed for the deepagents engine

# stdio — SonarFix starts the server as a subprocess
SONARFIX_SONAR_MCP=stdio
SONARFIX_SONAR_MCP_COMMAND=docker
SONARFIX_SONAR_MCP_ARGS=run -i --rm -e SONARQUBE_URL -e SONARQUBE_TOKEN mcp/sonarqube

# http — SonarFix connects to one you already run
SONARFIX_SONAR_MCP=http
SONARFIX_SONAR_MCP_URL=http://localhost:9000/mcp
```

`SONARFIX_SONAR_MCP_ARGS` takes a JSON list or a shell-style string. Confirm the
connection and see which tools the server offers with `sonarfix check --live`.

Install everything at once with `uv sync --extra all`.

---

## Verifying changes

Two scripts, neither of which needs credentials or spends anything:

```bash
uv run python scripts/smoke_test.py    # the workflow, end to end
uv run python scripts/config_test.py   # provider / engine / MCP configuration
```

`smoke_test.py` runs the real graph — the approval interrupt and resume, the git
branch/diff/commit path and every SQLite write — against a throwaway repository
with the engine stubbed at `engine.run_analysis` / `engine.run_fix`, so it covers
whichever engine you have configured.

`config_test.py` covers the configuration surface: provider selection and
defaults, the local-model clients, engine validation, and the MCP config shapes
for both transports.

## Layout

```
src/sonarfix/
  core/
    config.py       Settings from the environment
    store.py        SQLite: projects, issues, fix_plans, fix_runs
    sonar.py        SonarQube REST API (projects, issues, rule descriptions)
    repo.py         git: branch, stage, diff, commit, code slices
    providers.py    Model factory — anthropic / ollama / openai-compatible
    mcp.py          Optional SonarQube MCP server, shared by both engines
    agents.py       Output schemas and the two deepagents agents
    claude_code.py  The Claude Agent SDK engine
    engine.py       Engine dispatch + the async/sync bridge
    graph.py        The LangGraph workflow and its nodes
    service.py      Use cases — the only thing the API and UI call
  api/main.py       FastAPI routes
  ui/app.py         Streamlit, three screens
prompts/
  analyze.md        System prompt for the analyst
  fix.md            System prompt for the engineer
scripts/
  smoke_test.py     Workflow, end to end, with the engine stubbed
  config_test.py    Provider / engine / MCP configuration
```

The LangGraph checkpointer shares the same SQLite file as the application
tables, so there is one database and no extra infrastructure.

## Endpoints

| Method | Path | Step |
|---|---|---|
| `GET` | `/health` | status plus the active engine/provider/MCP wiring |
| `POST` | `/projects/sync` | 1 — refresh the project list |
| `PUT` | `/projects/{key}/repo` | point at a local clone |
| `POST` | `/projects/{key}/sync-issues` | 2 — fetch issues |
| `GET` | `/projects/{key}/issues` | 3 — list, filter by `severity` / `type` |
| `GET` | `/projects/{key}/facets` | filter options |
| `GET` | `/issues/{id}` | issue, rule text, code at the reported line |
| `POST` | `/issues/{id}/analyze` | 4–6 — analyse and plan, then park for approval |
| `GET` | `/issues/{id}/state` | current workflow state (survives restarts) |
| `POST` | `/issues/{id}/approve` | 7–10 — fix, commit message, PR description |
| `POST` | `/issues/{id}/reject` | 7 — reject the plan |

## Scope

Single developer, no auth, no RBAC, no multi-tenancy. Bitbucket, Azure Repos and
GitHub integration is deliberately left out: SonarFix commits locally and you
open the PR, so there is no source-control credential to configure. Adding a
"create PR" call later is one module.

Re-analysing an issue starts a clean run and writes a new `fix_plans` row; older
plans stay as history.
