"""Agent-Skill-Toolkit runtime package (docs/design/agent-ecosystem.md §B).

    core/          generic primitives: paths, durable store
    integrations/  service clients (no agent launching, no cross-integration imports)
    workflows/     execution (agent runners, quota), engine, domain workflows

Import rules are in §B.2. Skills under .claude/skills/<skill>/tools/ are thin facades.
"""

__version__ = "0.1.0"
