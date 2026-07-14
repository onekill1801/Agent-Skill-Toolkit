# aTask: Browse Projects & Sprints

List projects, sprints, workspaces, and boards in aTask.

## Arguments

`$ARGUMENTS` — optional filter. Examples:
- `/atask-projects` → list all my projects
- `/atask-projects Payment` → filter projects by name "Payment"

## Steps

1. Read `@.claude/skills/atask-automation/SKILL.md`
2. Read `@.claude/skills/atask-automation/cookbook/project-sprint-navigation.md`
3. Read `@.claude/skills/atask-automation/tools/projects.py`
4. Run `python3 projects.py my-projects` (with `--filter "$ARGUMENTS"` if provided)
5. Display projects in a table: ID | Name | Description
6. Ask: "Want to see sprints or lists for any project?"
7. If yes:
   - Sprints: `python3 projects.py sprints <project_id>`
   - Lists: ask for workspace ID → `python3 projects.py lists <workspace_id>`
8. Offer to drill further: tasks in a sprint, tasks in a list
