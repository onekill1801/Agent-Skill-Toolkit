# Bug Fix Workflow

## Purpose
End-to-end workflow for an AI agent to fix a bug: from reading the Azure DevOps task
to creating a merge request and notifying the tester.

## Prerequisites
- Azure DevOps task ID with type "Bug"
- Access to the GitLab repository
- Understanding of the project structure

## Workflow Steps

### Phase 1: Understand the Bug

1. **Read the task:**
   ```
   python azure_devops.py get <task_id>
   ```
2. **Extract key information:**
   - Bug title and description
   - Steps to reproduce
   - Expected vs actual behavior
   - Acceptance criteria
   - Severity/priority
   - Related work items (parent story, linked tasks)

3. **Analyze with task_analysis_prompt.md:**
   - Fill in the template with extracted info
   - Identify affected components/services
   - Determine root cause hypothesis

### Phase 2: Investigate the Code

4. **Locate the relevant code:**
   - Search for classes/methods mentioned in the bug description
   - Trace the execution flow from the entry point (controller/API)
   - Identify the exact location where the bug occurs

5. **Understand the context:**
   - Read related test files
   - Check recent changes to the affected files (git log)
   - Review related configuration files

### Phase 3: Implement the Fix

6. **Create the branch:**
   ```
   python gitlab_api.py create-branch "bugfix/<task_id>-<short-desc>" develop
   ```

7. **Notify the team:**
   ```
   python notifier.py started <task_id> "bugfix/<task_id>-<short-desc>"
   python azure_devops.py state <task_id> Active
   ```

8. **Write the fix:**
   - Fix the root cause, not just the symptom
   - Follow `cookbook/java-standards.md`
   - Keep changes minimal and focused on the bug
   - Do NOT refactor unrelated code in the same branch

9. **Write/update tests:**
   - Add a test that reproduces the bug (should fail without fix)
   - Ensure the test passes with the fix
   - Check that existing tests still pass

### Phase 4: Create Merge Request

10. **Commit and push changes** (via git commands in the project directory)

11. **Create the MR:**
    ```
    python gitlab_api.py create-mr "bugfix/<task_id>-<short-desc>" "Fix: <bug_title>" develop
    ```
    MR description should include:
    - Link to Azure DevOps task
    - Root cause analysis
    - What was changed and why
    - How to test the fix

12. **Self-review the MR:**
    - Use Workflow 1 (Review Merge Request) on the newly created MR
    - Fix any issues found before notifying testers

### Phase 5: Notify

13. **Notify MR created:**
    ```
    python notifier.py mr-created <task_id> <mr_url>
    ```

14. **If deployment is automated, notify after deploy:**
    ```
    python notifier.py deploy-done <task_id> <dev_env_url>
    ```

## MR Description Template

```markdown
## Bug Fix: <title>

**Azure DevOps Task:** #<task_id>
**Branch:** bugfix/<task_id>-<short-desc>

### Root Cause
<Explain what was causing the bug>

### Solution
<Explain what was changed and why>

### Changes
- `path/to/file.java` - <brief description of change>

### Testing
- [ ] Added regression test for the bug scenario
- [ ] Existing tests pass
- [ ] Manually verified the fix (describe steps)

### Acceptance Criteria
<Copy from Azure DevOps task>
```

## Error Recovery

| Situation | Action |
|-----------|--------|
| Cannot reproduce the bug | Comment on task asking for more details, set state to "Need Info" |
| Bug is in a dependency | Comment findings, create a separate task for dependency update |
| Fix requires schema changes | Include migration script, note in MR description |
| Multiple bugs in one task | Fix primary bug, create sub-tasks for related issues |
| Tests fail after fix | Investigate if tests are outdated or fix has side effects |
