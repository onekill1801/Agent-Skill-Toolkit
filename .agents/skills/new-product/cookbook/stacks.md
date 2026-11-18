# Supported stacks

`scaffold.py recipes` prints this list live. `scaffold.py check <stack>` tells you
whether it can run on THIS machine (CLI present / network reachable / offline-ready).

| stack | method | needs | build / test |
|-------|--------|-------|--------------|
| `java-spring` | http_zip | internet (start.spring.io) | `mvn -B -q -DskipTests package` / `mvn -B test` |
| `next` | cli | `npx` (Node.js) | `npm run build` / `npm test` |
| `react-vite` | cli | `npm` (Node.js) | `npm run build` / `npm test` |
| `node-express` | template | none (offline) | `npm run build` / `npm test` |
| `fastapi` | template | none (offline) | `pip install -e .` / `pytest` |
| `python-cli` | template | none (offline) | — / `pytest` |
| `static` | template | none (offline) | — / — |

## Methods explained

- **http_zip** — downloads a generated starter over HTTPS (`urllib`) and unpacks it
  (`zipfile`). No external CLI required. Used for `java-spring` via Spring Initializr.
  If offline, `run` returns a clear error — hand-write `pom.xml` + a main class instead.

- **cli** — shells out to the ecosystem's official generator (`create-next-app`,
  `create vite`). Requires Node.js on PATH. `check` reports the resolved path or a
  "not on PATH" note; on Windows it also probes the `.cmd` shim.

- **template** — writes a small, opinionated starter embedded in `scaffold.py`. Fully
  offline and deterministic. The right choice when there's no canonical generator, or
  when the machine lacks the CLI, or when you want a minimal skeleton with no extras.

## Choosing a stack when the user hasn't

1. Match the requirement to a language family (JVM service → `java-spring`; TS web app
   → `next`; quick Python API → `fastapi`; internal tool → `python-cli`; landing page
   → `static`).
2. Run `check` on the candidate. If `ready: false` (missing CLI), either install it,
   pick an offline-equivalent (`next` → `static`/hand-written; `react-vite` → `static`),
   or hand-write — but say which you did.
3. `java-spring` matches the repo's `java-standards.md` conventions and needs no local
   toolchain to scaffold (only to build), so it's the safe default for Java shops.

## Adding a new stack

Extend `RECIPES` in `scaffold.py`:
- `method: "template"` → add a `_tpl_<stack>` writer and register it in `_TEMPLATES`.
- `method: "cli"` → add a branch in `_run_cli` building the generator command.
- `method: "http_zip"` → add a `_run_<stack>` that fetches + unpacks a starter.

Keep it stdlib-only and return the same JSON shape.
