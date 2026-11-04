# Task Analysis Template

Use this template to structure the analysis of an Azure DevOps work item before
starting implementation. Fill in each section based on the task details.

---

## Task Summary

```yaml
task_id: <fill_in>
title: <fill_in>
type: <Bug | User Story | Task | Feature>
state: <New | Active | Resolved>
assigned_to: <fill_in>
priority: <fill_in>
```

## Requirements Analysis

### Description
<Paste or summarize the task description here>

### Acceptance Criteria
<List each acceptance criterion as a numbered item>
1. ...
2. ...
3. ...

### Technical Requirements
<Derived from acceptance criteria — what needs to happen technically>
- [ ] ...
- [ ] ...

## Impact Analysis

### Affected Components
| Layer | Files / Classes | Change Type |
|-------|----------------|-------------|
| Controller | | New / Modified |
| Service | | New / Modified |
| Repository | | New / Modified |
| Entity | | New / Modified |
| DTO | | New / Modified |
| Config | | New / Modified |
| Test | | New / Modified |

### Database Changes
- [ ] No schema changes needed
- [ ] New table(s): ...
- [ ] Modified table(s): ...
- [ ] New column(s): ...
- [ ] Migration script needed

### Dependencies
- External services affected: ...
- Other modules affected: ...
- Configuration changes: ...

## Implementation Plan

### Step-by-step
1. ...
2. ...
3. ...

### Risk Assessment
| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| ... | Low/Medium/High | ... |

### Estimated Complexity
- [ ] Small (1-3 files, < 100 lines)
- [ ] Medium (4-10 files, 100-500 lines)
- [ ] Large (10+ files, 500+ lines) — consider breaking into sub-tasks

## Branch Name
`<type>/<task_id>-<short-kebab-description>`

## MR Title
`<Type>: <Task Title>`
