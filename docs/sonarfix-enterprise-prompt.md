# Prompt: SonarFix Enterprise — cost-aware, human-in-the-loop Sonar remediation

You are the lead AI architect and implementer of **SonarFix Enterprise**. It is an agentic
platform that fetches SonarQube issues, groups them, generates fixes, checks those fixes with
real builds and tests, and opens pull requests for review. It targets Java, C# and C++
repositories that have 1,000 or more open issues.

You are extending an existing, working MVP. Do not start over.

---

## 0. Guiding principles (these override everything below)

1. **Deterministic first, LLM last.** Anything that can be done without a model is done
   without one: fetching, parsing, deduplication, prioritization, routing, building, testing,
   diffing and PR creation. The LLM is used only where judgement is needed.
2. **Lazy activation.** No agent is instantiated, and no model is called, until a queued work
   item needs it. If a repository has no C++ code, the C++ agents never load. If a cluster is
   pure code-style, the security agent never runs.
3. **Fix once, apply many.** For a cluster of similar issues, the LLM fixes one
   representative instance. The fix is turned into a reusable transformation and applied to
   the other instances. Each application is checked, and a model is called again only for
   instances where the transformation fails.
4. **Budgets are hard limits.** Every run has a token and cost budget that it cannot exceed.
   When the budget is reached, the run stops cleanly with a resumable checkpoint. It never
   continues quietly.
5. **Humans own every irreversible step.** Nothing is pushed, no PR is opened, and no
   security-sensitive change is proposed without explicit human approval. Nothing is ever
   auto-merged.
6. **Say when unsure.** If a finding is a false positive, is already fixed, or would be
   unsafe to change, record that with a reason. Never produce a change just to close an
   issue.

---

## 1. What already exists (reuse it)

The MVP in this repository already provides:

- `core/sonar.py`: SonarQube/SonarCloud client, with an org parameter, truststore TLS and
  ALM binding discovery (GitHub, GitLab, Bitbucket).
- `core/graph.py`: a LangGraph workflow with a SQLite checkpointer and a durable approval
  interrupt (`interrupt()` → `Command(resume=...)`).
- `core/engine.py`: the single engine seam (`run_analysis`, `run_fix`, `run_pr_text`)
  that dispatches to either `deepagents` or the `claude-code` SDK.
- `core/providers.py`: Anthropic (API key or OAuth bearer), Ollama and OpenAI-compatible
  local models.
- `core/repo.py`: git workspace handling (clean-tree check, branch, stage, diff, commit).
- `core/report.py`: a self-contained HTML report.
- `api/main.py` (FastAPI) and `frontend/` (React and Vite).
- `prompts/analyze.md` and `prompts/fix.md`: honest-outcome prompts that allow a
  "no change needed" result.

Evolve these modules. Keep the engine seam, so every new agent calls models only through it.

---

## 2. Architecture

```
Orchestrator (LangGraph, checkpointed, resumable)
│
├── Stage A — Deterministic (no LLM)
│   ├── Fetch        Sonar API → normalized issue records, incremental by updateDate
│   ├── Filter       skip resolved / already-fixed / won't-fix / generated / vendored paths
│   ├── Dedup        cluster by (rule_key, normalized code fingerprint)
│   ├── Prioritize   deterministic score → ranked cluster queue
│   └── Plan         batches + cost estimate  ──► HITL GATE 1 (approve run plan & budget)
│
├── Stage B — Mechanical fixers (no LLM)
│   └── Java: OpenRewrite · C#: dotnet format / Roslyn code fixes · C++: clang-tidy --fix
│
├── Stage C — LLM fixers (lazy, per cluster)
│   ├── Triage       cheap model: confirm real issue? category? confidence?
│   ├── Language router → {Java, C#, C++} → {Security | Performance | Quality | Memory}
│   ├── Representative fix  ──► HITL GATE 2 (approve fix pattern for the cluster)
│   └── Propagate    apply pattern to remaining instances; LLM only on failures
│
├── Stage D — Validation (no LLM, except for error repair)
│   ├── Build        incremental: affected modules/projects only
│   ├── Test         impacted tests first, then the module suite
│   └── Repair loop  max 2 LLM attempts fed with the compiler/test error, then give up
│
└── Stage E — Delivery
    ├── PR Agent     one PR per batch  ──► HITL GATE 3 (approve push + PR creation)
    └── Reporting    run metrics, cost ledger, HTML + JSON
```

### Agent registry and lazy loading

- Keep a registry that maps `(language, category)` to an agent factory. Instantiate an
  agent on first use and cache it for the rest of the run.
- Detect languages once per repository from file extensions and build files (`pom.xml`,
  `build.gradle`, `*.csproj`/`*.sln`, `CMakeLists.txt`). Register only the language agents
  whose language is present.
- Each sub-agent is **a system prompt plus a tool allow-list plus a model tier**. It is not a
  separate service. Adding one is a configuration change.

---

## 3. Stage A — deterministic pipeline

**Fetch.** Page through `/api/issues/search` with the existing client. Store each issue with
`issue_key, language, rule_key, severity, type, file_path, line, message, sonar_hash,
update_date`. Re-runs are incremental: fetch only issues updated since the last successful
run. Cache rule metadata per `rule_key` for the whole run.

**Filter.** Drop these issues before any other processing:
- issues whose local file content no longer matches (the line is gone, or the file hash has
  changed and the Sonar line context is missing)
- issues on paths matching `generated/`, `vendor/`, `third_party/`, `*.pb.*` or paths
  listed in a user-configurable ignore file
- issues with an existing fix record: same `(rule_key, file, fingerprint)` in a merged or
  open PR

**Dedup and cluster.** Grouping has two levels:
1. `rule_key` (exact).
2. Inside each rule, a **code fingerprint**: take the flagged line ±3 lines, normalize
   identifiers, literals and whitespace (with tree-sitter where it is available, regex
   otherwise), then hash the result. Identical fingerprints form one cluster.

Embeddings (ChromaDB with a local embedding model) are **optional**. Use them only to merge
leftover singleton clusters within the same rule, and never as the first pass.
Target: 1,000 issues become about 150 clusters.

**Prioritize.** Use a deterministic score with no LLM:
```
score = severity_weight            # BLOCKER 100, CRITICAL 60, MAJOR 30, MINOR 10, INFO 2
      + security_bonus             # VULNERABILITY / SECURITY_HOTSPOT: +50
      + log2(cluster_size) * 10    # frequency
      + business_weight(path)      # user-configurable path → weight map
      - effort_penalty             # e.g. cognitive-complexity refactors: -20
```

**Plan and estimate.** Form batches of related clusters. A batch belongs to one module, stays
within the PR size limits in §7, and has one fix category. For each batch, estimate:
- the number of LLM calls: 0 for mechanical fixes, 1 triage call plus 1 representative fix
  plus the expected failure rate × instances for LLM fixes
- tokens and cost by model tier, from the pricing table in configuration

Show the plan in the UI. **HITL Gate 1:** a human approves, edits or trims the plan and sets
the run budget. Nothing past Stage A runs without this approval.

---

## 4. Stage B — mechanical fixers (do these before any LLM work)

Many Sonar rules have safe, deterministic fixes. Keep a rule map in `config/mechanical_rules.yaml`
(an example is `java:S1128` unused import → OpenRewrite `RemoveUnusedImports`). For those
rules, run the tool, build and test, and **skip the LLM entirely**. This stage should handle
a large share of the backlog at almost no cost. Report how many issues it resolved.

---

## 5. Stage C — LLM fixers

### Model tiering

Configure the tiers. Never hard-code models.

| Tier | Used for | Default |
|------|----------|---------|
| `triage` | real issue? category? confidence? | small/cheap model or a local LLM |
| `fix` | representative fixes, repair loop | mid-tier model |
| `expert` | security fixes, escalation after a failed repair | top-tier model, only when needed |

Escalation is one-way and explicit. A task goes from `fix` to `expert` only after a failed
validation, and only for clusters in the security or memory categories. Every escalation is
recorded in the cost ledger.

### Triage (one cheap call per cluster, not per issue)

Input: the rule description (cached), one representative snippet, and the cluster size.
Output (structured):
`{real_issue: bool, category: security|performance|quality|memory, confidence: 0-1,
 approach: string, risk: low|medium|high}`
- If `real_issue=false`, mark the cluster as a false positive with a reason. No fix is
  attempted, and the item is queued for a human to resolve in Sonar.
- If `confidence < 0.6` or `risk=high`, send the cluster to a human before any fix is
  generated.

### Specialist agents

Each specialist is a system prompt plus a tool allow-list. Its scope:
- **Java Security:** SQL injection, deserialization, XSS, path traversal, hardcoded secrets.
  Follow the OWASP secure-coding guidance. Outputs a patch, its reasoning and a risk level.
- **Java Performance:** allocations, loops, stream misuse, N+1 database calls,
  try-with-resources.
- **Java Quality:** code smells, null safety, naming, duplication, unused code.
- **C# Security, Performance and Quality:** .NET, ASP.NET Core and Entity Framework
  idioms, `IDisposable`/`using`, async/await correctness.
- **C++ Memory:** leaks, dangling pointers, double free, invalid access, converting raw
  pointers to smart pointers, RAII. Target C++17 or C++20.
- **C++ Performance and Quality:** moves and copies, STL algorithm use, const-correctness.

All specialists must:
- read only the files they need, using the smallest useful slice (the function, not the
  whole file)
- make the **smallest change** that resolves the rule, with no drive-by refactors
- return `{patch, reasoning, risk, confidence, no_change_needed: bool, reason}`

### Fix once, apply many

1. Pick the most typical instance in the cluster as the representative.
2. The specialist fixes it. **HITL Gate 2:** a human reviews the representative diff and
   approves the fix pattern for the whole cluster. Security clusters always stop here.
   Quality clusters above a configurable confidence threshold can be set to auto-approve.
3. Turn the approved fix into a transformation: an OpenRewrite/Roslyn/clang-tidy recipe
   where one is possible, otherwise a structural search-and-replace template.
4. Apply the transformation to the other instances. Any instance where it fails to apply
   gets one `fix`-tier LLM call, with the approved representative diff included as a
   few-shot example.

### Caching

- **Prompt caching:** system prompts, rule descriptions and few-shot examples go first in
  the prompt so they are cache hits.
- **Analysis cache:** keyed by `(rule_key, fingerprint, model_tier, prompt_version)`.
  A hit reuses the result with no call.
- **Fix library:** approved transformations are stored per `(rule_key, fingerprint)` and
  reused in later runs and in other repositories.

---

## 6. Stage D — validation

- **Incremental build:** only affected modules, for example `mvn -pl <module> -am -q
  compile`, `gradle :<module>:compileJava`, `dotnet build <project>.csproj`, or
  `cmake --build <dir> --target <target>`. Run a full build once per batch before the PR.
- **Tests:** run impacted tests first (from a test-to-source mapping or a naming
  convention), then the module suite. **Never open a PR when tests fail.** A test that was
  already failing before the change is recorded as a baseline failure, not blamed on the fix.
  Establish the baseline once per run.
- **Repair loop:** at most 2 attempts. Each attempt sends the model only the compiler or
  test error and the diff, never the whole log. After 2 failures, revert that instance, mark
  it `needs_human` and continue the batch without it.
- Commands, timeouts and environments are configured per repository. Validation runs in an
  isolated working copy (a git worktree or a container), never in the user's checkout.

---

## 7. Stage E — delivery

**PR Agent.** Runs one PR per batch, only after **HITL Gate 3** approval.
- Size limits (configurable): no more than 20 files, no more than 400 changed lines, and one
  fix category per PR. Split a batch that goes over these limits.
- Supported platforms: Bitbucket (the primary target), GitHub and Azure DevOps. Put them
  behind a `ScmProvider` interface with the methods `push_branch`, `open_pr`, `add_labels`
  and `add_comment`. Credentials come from the environment or a secret store and are never
  placed in prompts.
- Never push to protected or default branches. Always use a `sonarfix/<batch-id>` branch.
- **Never auto-merge.** Every PR waits for human review.
- PR template:
  - Summary
  - Issues fixed (Sonar keys with links)
  - Files modified
  - Risk assessment
  - Build status
  - Test results (including baseline failures)
  - Issues deliberately **not** fixed, with reasons

**Reporting Agent.** Produces an HTML and JSON report for each run, extending
`core/report.py`, with:
- total issues fetched, filtered, clustered, fixed mechanically, fixed with an LLM, and
  marked false positive
- files changed, build and test status, and PR URLs
- the **cost ledger:** tokens and dollars per stage, agent, tier and batch, plus cache hit
  rate and cost per fixed issue

---

## 8. Human-in-the-loop summary

| Gate | When | A human can |
|------|------|-------------|
| 1 — Run plan | after Stage A | approve, trim batches, set the budget, pick languages and categories |
| 2 — Fix pattern | after the representative fix | approve, edit or reject the pattern; add reviewer notes that override the plan |
| 3 — Delivery | after validation | approve the push and PR, or hold |
| Escalations | any time | review false positives, low-confidence items, security changes, repair failures |

Gates use the existing LangGraph `interrupt()` and checkpointer, so a run can wait for hours
or days and resume exactly where it stopped. The UI shows a single **review queue** that
lists every pending decision across runs.

---

## 9. Cost and safety controls (required)

- A per-run budget (`max_usd`, `max_tokens`) and a per-cluster cap. When either is reached,
  checkpoint and pause. Never continue past it.
- **Dry-run mode:** Stage A plus cost estimate only. It makes zero LLM calls.
- **Concurrency limits:** a semaphore on LLM calls (default 3) and on builds (default 2).
  Parallelize across batches, not across issues in one file.
- **Kill switch:** `POST /runs/{id}/stop` stops the run cleanly with a checkpoint.
- Agents write only inside the isolated worktree. Paths are resolved and checked against the
  repository root, symlinks are rejected, and there is no network access from fix agents.
- Secrets never enter prompts. Scan diffs for secrets before any push.
- Record the tier, tokens and cost of every LLM call in the ledger table.

---

## 10. Technology

- Backend: Python and FastAPI (existing).
- Orchestration: LangGraph (existing). Use subgraphs per stage and `Send` for parallel
  batches.
- LLMs: through the existing engine seam. Claude Code SDK, the Claude API, OpenAI and local
  models (Ollama or OpenAI-compatible) are all supported.
- Database: PostgreSQL. Migrate from SQLite with Alembic and keep SQLite for local dev.
  Use a LangGraph Postgres checkpointer.
- Vector store: ChromaDB, **optional**, for singleton merging and fix-library search only.
- SCM: Bitbucket first, then GitHub and Azure DevOps.
- CI hooks: Jenkins, GitHub Actions and Azure DevOps can trigger a dry run and post the plan
  for approval.

---

## 11. Delivery phases (build in this order; do not build everything at once)

1. **Phase 1 — Java, deterministic core.** Stage A, the Java mechanical fixers, Gate 1,
   dry-run mode, the cost ledger and the Postgres migration.
   *Exit:* 1,000 issues become a clustered, costed plan with zero LLM calls.
2. **Phase 2 — Java LLM fixers.** Triage, the three Java specialists, fix-once-apply-many,
   Gate 2, incremental build and test, and the repair loop.
3. **Phase 3 — Delivery.** The Bitbucket PR agent, Gate 3, the review queue and the
   extended report. After that, the GitHub and Azure DevOps providers.
4. **Phase 4 — C#.** Language detection, `dotnet format`/Roslyn fixers and the C#
   specialists.
5. **Phase 5 — C++.** clang-tidy fixers, the C++ Memory, Performance and Quality agents,
   and CMake validation.
6. **Phase 6 — Scale and learning.** The cross-repository fix library, embeddings for
   singleton merging, and tuning the auto-approve thresholds from reviewer decisions.

Each phase ships with tests. Use fixtures for Sonar responses, a small sample repository
per language, and stubbed engine calls (follow the pattern in `scripts/smoke_test.py`).
Report the estimated cost of the phase's reference run. Do not start the next phase until
the current phase's exit criteria pass.

---

## 12. Success criteria

- 1,000 or more issues produce a costed plan in minutes, with no LLM calls.
- At least 30% of the fixed issues are resolved by mechanical fixers.
- LLM calls grow with the number of **clusters**, not the number of issues.
- Every PR builds and passes tests, stays within the size limits, and waits for review.
- Cost per fixed issue is visible, and the budget is never exceeded.
- False positives are reported, not "fixed".

---

## Your first response

Do not write code yet. Return:
1. A gap analysis between the current MVP modules (§1) and Phase 1.
2. The Phase 1 data model: tables for runs, issues, clusters, batches, fixes, gates and
   ledger.
3. The LangGraph state schema and the node list for Stage A and Gate 1.
4. A cost estimate method with a worked example for 1,000 Java issues.
5. Open questions that need a human decision before you start.
