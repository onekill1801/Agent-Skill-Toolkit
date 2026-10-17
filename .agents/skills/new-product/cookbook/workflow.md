# Workflow: from requirement to a registered greenfield project

A worked example. Product: "a small REST service that stores customer feedback".

## 1. Intake

- Name: `feedback-svc` (kebab-case; becomes `work/feedback-svc/` and the GitLab leaf).
- Ingest the requirement — **from a file / paste / task, not the CLI argument** when it's
  long:

```
# from a spec file
python intake.py build --name feedback-svc --file ../../../REQUIREMENT.md
# or paste / pipe (e.g. an Azure task) through stdin
python ../../dev-automation/tools/azure_devops.py get 6955 | python intake.py build --name feedback-svc -
# short one-liner
python intake.py build --name feedback-svc --text "store customer feedback via a REST API"
```

This writes `temp/products/feedback-svc/requirement.md` (verbatim) + `brief.md`
(template with auto-detected capability bullets). **Read `requirement.md` fully.**

## 2. Clarify (2–4 questions max)

Resolve the **stack** here — the user has not pre-picked one.

```
python scaffold.py recipes          # what can I scaffold?
python scaffold.py check fastapi     # is it buildable on this machine?
python ../../dev-automation/tools/doctor.py   # which CLIs exist (npx, mvn, ...)
```

Blocking questions worth asking:
- Language/stack? (propose one from `recipes`, confirmed by `check`)
- Persistence — Postgres / SQLite / none for now?
- Public surface — REST API / web UI / CLI / library?
- Publish to GitLab now, or stay local?

## 3. Design checkpoint

Fill in the `temp/products/feedback-svc/brief.md` that Intake generated. The essential
move for a long spec: **split the auto-detected capabilities into skeleton vs backlog.**

```markdown
## Stack
Python + FastAPI (check: ready, offline template)

## Data / entities
Feedback { id, customer, message, status, created_at }

## Capabilities
### Skeleton scope (build NOW)
- [x] /health + POST /feedback (in-memory) + one test
### Deferred backlog (→ /auto-dev, one per run)
- [ ] DB persistence (Postgres)      - [ ] list + filter by status
- [ ] email/password auth            - [ ] weekly report export
- [ ] email notify on new feedback
```

Present it. **Do not scaffold until approved.** Each backlog item later becomes a
`/auto-dev "<item> in feedback-svc"` run.

## 4. Scaffold

```
python scaffold.py run fastapi --name feedback-svc --dir ../../../work/feedback-svc
```

Then edit files to add the one design-specific endpoint + test. Keep it minimal.

Java example (no CLI needed — uses start.spring.io):

```
python scaffold.py run java-spring --name feedback-svc \
    --dir ../../../work/feedback-svc \
    --group com.example --java-version 21 --deps web,data-jpa,postgresql,validation
```

## 5. Verify

Use the `cmds` returned by `scaffold.py run`:

```
# FastAPI
python -m pip install -e ../../../work/feedback-svc && python -m pytest ../../../work/feedback-svc
# Spring
mvn -B test -f ../../../work/feedback-svc/pom.xml
```

Must be green before committing. Report the real output.

## 6. Deliver

```
# local commit (git identity must be set — tool tells you if not)
python repo_init.py init ../../../work/feedback-svc --branch main

# OPTIONAL, outward-facing — ask first:
python repo_init.py create-gitlab feedback-svc --visibility private --description "Customer feedback service"
#   -> returns id + http_url
python repo_init.py push ../../../work/feedback-svc <http_url> --branch main

# register so auto-dev can take over:
python registry.py add feedback-svc \
    --clone-dir ../../../work/feedback-svc \
    --stack fastapi --default-target-branch main \
    --gitlab-project-id <id> --gitlab-path <group>/feedback-svc \
    --build-cmd "python -m pip install -e ." --test-cmd "python -m pytest"
```

## 7. Hand off

> Skeleton ready at `work/feedback-svc` (build green, registered).
> Run `/auto-dev "add DB persistence for feedback in feedback-svc"` to build features.

## Path note

The tools live in `.claude/skills/new-product/tools/`. Paths above are relative to
that dir; from the repo root, use `work/feedback-svc` directly and
`python .claude/skills/new-product/tools/scaffold.py ...`.
