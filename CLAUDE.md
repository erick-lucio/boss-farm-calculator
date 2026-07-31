​# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Workflow Orchestration

### 1. Plan Node Default
- Enter plan mode for ANY non-trivial task (3+ steps or architectural decisions)
- If something goes sideways, STOP and re-plan immediately – don't keep pushing
- Use plan mode for verification steps, not just building
- Write detailed specs upfront to reduce ambiguity

### 2. Subagent Strategy
- Use subagents liberally to keep main context window clean
- Offload research, exploration, and parallel analysis to subagents
- For complex problems, throw more compute at it via subagents
- One task per subagent for focused execution
- **Model selection**: Use the cheapest model (`haiku`) for code reading, exploration, and research subagents — reserve `sonnet`/`opus` only for tasks that require writing or complex reasoning

### 3. Self-Improvement Loop
- After ANY correction from the user: update `tasks/lessons.md` with the pattern
- Write rules for yourself that prevent the same mistake
- Ruthlessly iterate on these lessons until mistake rate drops
- Review lessons at session start for relevant project

### 3b. Session Init — Pending Task Review
- At the **start of every session**, read `tasks/todo.md` (if it exists)
- Identify all **pending/incomplete** items
- For each pending item, assess whether you have enough context to begin analysis (codebase knowledge, DB access, etc.)
- Present a concise summary to the user: list the pending items, mark which ones you can start on, and ask: **"Should I start analyzing any of these?"**
- Do NOT begin work on any item until the user confirms

### 4. Verification Before Done
- Never mark a task complete without proving it works
- Diff behavior between main and your changes when relevant
- Ask yourself: "Would a staff engineer approve this?"
- Run tests, check logs, demonstrate correctness
- **After every edit, explicitly list which services/projects were changed** (e.g. `credit-limit-service`, `lbd-cmn-tkl-get`, `web/`)

### 5. Demand Elegance (Balanced)
- For non-trivial changes: pause and ask "is there a more elegant way?"
- If a fix feels hacky: "Knowing everything I know now, implement the elegant solution"
- Skip this for simple, obvious fixes – don't over-engineer
- Challenge your own work before presenting it

### 6. Autonomous Bug Fixing
- When given a bug report: just fix it. Don't ask for hand-holding
- Point at logs, errors, failing tests – then resolve them
- Zero context switching required from the user
- Go fix failing CI tests without being told how

### 7. Database Access Rules
- **NEVER** execute DDL (`CREATE`, `ALTER`, `DROP`, `TRUNCATE`) or write operations (`INSERT`, `UPDATE`, `DELETE`, `MERGE`) against any database under any circumstance
- **Only `SELECT` statements are permitted** — read-only, always
- **Always ask the user for explicit confirmation before running any `SELECT`** — describe what query you intend to run and wait for approval before executing it
- If the task seems to require a write, stop and tell the user — never attempt a workaround
- **Both databases (`petronas-postgres` and `petronas-sqlserver`) are on a VPN** — if any connection fails, immediately tell the user: "Connection failed — please check your VPN connection and try again"
- `petronas-sqlserver` (`Infocenter4`) relates to the **Infocenter monolith and console applications**
- **IMPORTANT:** `petronas-sqlserver` always connects to `master` by default (dbhub ignores the DSN `database` param) — **every SQL Server query must be prefixed with `USE Infocenter4;`**
- `petronas-postgres` (`myInfocentre`) relates to the **MyInfocentre microservices and Lambda functions**

---

## Task Management

1. **Plan First**: Write plan to `tasks/todo.md` with checkable items
2. **Verify Plan**: Check in before starting implementation
3. **Track Progress**: Mark items complete as you go
4. **Explain Changes**: High-level summary at each step
5. **Document Results**: Add review section to `tasks/todo.md`
6. **Capture Lessons**: Update `tasks/lessons.md` after corrections

---

## Core Principles

- **Simplicity First**: Make every change as simple as possible. Impact minimal code.
- **No Laziness**: Find root causes. No temporary fixes. Senior developer standards.
- **Minimal Impact**: Changes should only touch what's necessary. Avoid introducing bugs.

## Code Quality Standards

Apply these principles on every change, **always respecting and following the existing architecture of the project being modified**. Never restructure or refactor the project's architectural style — work within it.

- **DRY (Don't Repeat Yourself)**: Extract shared logic; never duplicate behavior, constants, or queries.
- **Single Responsibility**: Each class, method, and module does one thing only.
- **Open/Closed**: Extend behavior without modifying existing stable code.
- **Dependency Inversion**: Depend on abstractions (interfaces), not concrete implementations.
- **Clean Architecture layers**: Respect the existing layer boundaries (Api → Domain → Infra). Never let Infra leak into Domain or Domain into Api.
- **Meaningful names**: Variables, methods, and classes must be self-explanatory — no abbreviations, no cryptic names.
- **Small functions**: Keep methods short and focused; if a function needs a comment to explain what it does, it needs to be broken down.
- **Fail fast**: Validate inputs at boundaries; throw specific, meaningful exceptions early.
- **No magic numbers/strings**: Use named constants or enums.
- **Consistent patterns**: Match the existing patterns in the file/service being modified — don't introduce a new style in isolation.

## Git Rules

- **Branches**: Create branches locally only — NEVER commit or push unless explicitly asked by the user.
- **Pull before branch**: Always `git pull origin master` before creating a new branch.

## Code Style

- **No comments**: Never add comments to code. Code must be self-explanatory.

---

## Task Backlog

All pending implementation tasks are tracked in **[`tasks/todo.md`](tasks/todo.md)**.
Read this at the start of every session (§3b above). Never lose this reference.

---

## Change History

All change history is tracked in [`change.md`](change.md) at the repo root.
After every implemented change, append a new row to the relevant service table in that file.

---

## Architecture

Full system architecture (backend, frontend, pages, admin detection, security, static
deployment) lives in **[`ARCHITECTURE.md`](ARCHITECTURE.md)**, not here.

**Whenever a change affects the architecture** (new page, new data flow, new external
dependency, changed routing/redirect behavior, new Durable Object or storage pattern,
etc.), **update `ARCHITECTURE.md` in the same change** — do not let it drift out of date.
