# Merge Request Code Review Workflow

## Purpose
Guide the AI agent through a structured code review of a GitLab merge request,
ensuring code quality, security, and adherence to team standards.

## Pre-Review Checklist

Before reviewing code, gather context:
1. Read the MR description and linked Azure DevOps task (if any)
2. Understand the purpose of the change (bug fix, feature, refactor)
3. Check the target branch (develop, main, release/*)
4. Review the list of changed files to understand scope

## Review Categories

### 1. Correctness
- Does the code do what the task/ticket describes?
- Are edge cases handled (null, empty collections, boundary values)?
- Are error scenarios handled with proper exception types?
- Do conditional branches cover all cases?
- Are loops bounded and safe from infinite iteration?

### 2. Code Quality
- Are method and variable names descriptive and consistent?
- Are methods kept short (< 30 lines preferred)?
- Is there unnecessary code duplication?
- Are magic numbers replaced with named constants?
- Is the code self-documenting (minimal comments needed)?

### 3. Architecture & Design
- Does the change follow existing patterns in the codebase?
- Is the separation of concerns maintained (Controller -> Service -> Repository)?
- Are new dependencies justified?
- Is the change backward compatible?
- Are DTOs used properly (no entity leakage to API layer)?

### 4. Security
- Is user input validated and sanitized?
- Are SQL queries parameterized (no string concatenation)?
- Are sensitive fields excluded from logs and API responses?
- Are authorization checks in place for protected endpoints?
- Are secrets/tokens not hardcoded?

### 5. Performance
- Are database queries optimized (N+1 problem, missing indexes)?
- Are large collections processed with pagination or streaming?
- Is caching used where appropriate?
- Are unnecessary database calls avoided (lazy loading issues)?

### 6. Testing
- Are there unit tests for new business logic?
- Are edge cases covered in tests?
- Do tests follow Arrange-Act-Assert pattern?
- Are test names descriptive (should_returnEmpty_when_noItemsFound)?
- Are mocks used appropriately (not over-mocking)?

### 7. Configuration & Deployment
- Are new config properties documented?
- Are database migrations included if schema changed?
- Are feature flags used for risky changes?
- Is the change deployable independently?

## Review Output Format

Structure the review comment as follows:

```markdown
## Code Review Summary

**MR:** !<mr_iid> - <title>
**Reviewer:** AI Agent
**Overall:** [APPROVE / REQUEST_CHANGES / COMMENT]

### Critical Issues (must fix)
- [ ] [File:Line] Description of the issue

### Suggestions (should consider)
- [ ] [File:Line] Description of the suggestion

### Minor / Nitpicks
- [ ] [File:Line] Description

### Positive Observations
- Description of good practices observed
```

## Severity Levels

| Level | Action | Examples |
|-------|--------|---------|
| **Critical** | Block MR | Security vulnerability, data loss risk, logic error |
| **Major** | Request changes | Performance issue, missing validation, no tests |
| **Minor** | Suggest | Naming, style, minor refactoring opportunity |
| **Nitpick** | Comment only | Formatting, comment typos, import ordering |

## Tone Guidelines

- Be constructive, not confrontational
- Explain WHY something should change, not just WHAT
- Suggest specific improvements with code examples when possible
- Acknowledge good patterns and practices
- Use questions for subjective suggestions ("Have you considered...?")
