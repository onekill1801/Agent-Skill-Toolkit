# Generate Skill Prompt

Generate a complete skill tree from **`temp/tool-inventory.yaml`** and **`temp/skill-design.md`**.

## Prerequisites

- Both temp files exist and user approved the design (or said "generate").
- Read templates in `templates/` and reference skill @.claude/skills/dev-automation/

## Files to create

```
.claude/skills/<folder_name>/
├── SKILL.md                 # from templates/SKILL.md.template
├── tools/
│   ├── config.py            # reuse pattern from dev-automation if env_vars present
│   └── <domain>.py          # one module per domain, CLI subcommands
├── cookbook/
│   └── <workflow>.md        # one per workflow in design
└── prompts/
    └── <optional>.md        # only if workflows need fill-in templates
```

Optional:
- `.claude/commands/<cmd>.md` per slash command in design
- Append to `.env.sample` (placeholders only)
- Append steps to `.claude/commands/prime.md`
- Add row to `.claude/commands/all_skills.md`

## Generation rules

### SKILL.md

- YAML frontmatter: `name`, multi-line `description` with trigger phrases.
- Sections: Feature Flags, Available Tools table, Workflows (numbered), Routing table, Slash Commands, Important Rules.
- Point agent to `cd` into `tools/` before running Python.
- Document `python3` vs `python` and `SSL_VERIFY` if HTTPS to corporate hosts.

### tools/*.py

- Shebang `#!/usr/bin/env python3`
- Stdlib only unless design explicitly allows `requests` (prefer urllib).
- Each inventory tool → function + CLI subcommand.
- `if __name__ == "__main__"` with `argparse` or simple `sys.argv` (match dev-automation style).
- Import `config` from same directory for env loading.
- Return JSON to stdout; UTF-8 safe printing on Windows (`sys.stdout.buffer.write`).

### cookbook/*.md

- Purpose, Prerequisites, numbered Steps with exact CLI lines.
- Error recovery table at bottom.
- MR/description templates only if workflow creates PRs.

### Do not

- Copy proprietary app source wholesale — only thin wrappers calling HTTP/CLI.
- Commit `temp/` contents with secrets.
- Hardcode user org URLs with real tokens.

## Verification checklist (run after generate)

```bash
cd .claude/skills/<folder_name>/tools
python3 config.py          # if present
python3 <main>.py --help   # or print docstring
```

Report to user: files created, example natural-language triggers, example slash commands, next step to test with `claude` + `/prime`.
