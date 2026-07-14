# New Feature Implementation Workflow

## Purpose
End-to-end workflow for an AI agent to implement a new feature: from reading the
Azure DevOps task to creating a merge request and notifying the team.

## Prerequisites
- Azure DevOps task ID with type "User Story", "Task", or "Feature"
- Access to the GitLab repository
- Understanding of the project architecture

## Workflow Steps

### Phase 1: Understand the Requirements

1. **Read the task:**
   ```
   python azure_devops.py get <task_id>
   ```

2. **Extract key information:**
   - Feature title and description
   - Acceptance criteria (this is the source of truth)
   - UI/UX mockups or references (if linked)
   - Dependencies on other tasks
   - Parent story/epic for broader context

3. **Analyze with task_analysis_prompt.md:**
   - Break down acceptance criteria into technical requirements
   - Identify affected layers (API, Service, Repository, Database)
   - List files that need to be created or modified
   - Identify potential risks or unknowns

4. **Check related context:**
   - Read parent story for overall feature direction
   - Check linked tasks for dependencies
   - Review existing code patterns for similar features

### Phase 2: Plan the Implementation

5. **Create an implementation plan:**
   - Database changes (entities, migrations)
   - Repository layer (new queries, interfaces)
   - Service layer (business logic)
   - Controller/API layer (endpoints, DTOs)
   - Tests for each layer
   - Configuration changes

6. **Identify the order of implementation:**
   - Database/Entity first
   - Repository second
   - Service third
   - Controller/DTO last
   - Tests alongside each layer

### Phase 3: Implement the Feature

7. **Create the branch:**
   ```
   python gitlab_api.py create-branch "feature/<task_id>-<short-desc>" develop
   ```

8. **Notify the team:**
   ```
   python notifier.py started <task_id> "feature/<task_id>-<short-desc>"
   python azure_devops.py state <task_id> Active
   ```

9. **Implement layer by layer** following `cookbook/java-standards.md`:

   **a. Database Layer (if needed):**
   - Create/modify entity classes
   - Create migration scripts (Flyway/Liquibase)

   **b. Repository Layer:**
   - Create/modify repository interfaces
   - Add custom queries if needed

   **c. Service Layer:**
   - Implement business logic in service classes
   - Use interfaces for dependency injection
   - Handle error cases with custom exceptions

   **d. API Layer:**
   - Create/modify DTOs (request and response)
   - Create/modify controller endpoints
   - Add input validation (@Valid, custom validators)

   **e. Tests:**
   - Unit tests for service layer (mock repositories)
   - Integration tests for repository layer
   - Controller tests (MockMvc or WebTestClient)

### Phase 4: Create Merge Request

10. **Commit and push changes** with meaningful commit messages

11. **Create the MR:**
    ```
    python gitlab_api.py create-mr "feature/<task_id>-<short-desc>" "Feature: <title>" develop
    ```

12. **Self-review the MR** using Workflow 1 (Review Merge Request)

### Phase 5: Notify

13. **Notify MR created:**
    ```
    python notifier.py mr-created <task_id> <mr_url>
    ```

14. **After deployment:**
    ```
    python notifier.py deploy-done <task_id> <dev_env_url>
    ```

## MR Description Template

```markdown
## Feature: <title>

**Azure DevOps Task:** #<task_id>
**Branch:** feature/<task_id>-<short-desc>

### Overview
<Brief description of the feature>

### Implementation Details
<Explain architecture decisions and key implementation details>

### Changes
- `path/to/NewEntity.java` - New entity for <purpose>
- `path/to/Service.java` - Business logic for <feature>
- `path/to/Controller.java` - API endpoints: GET /api/..., POST /api/...

### New API Endpoints
| Method | Path | Description |
|--------|------|-------------|
| GET | /api/... | ... |
| POST | /api/... | ... |

### Database Changes
- New table: `table_name` (describe columns)
- Migration: `V<version>__<description>.sql`

### Testing
- [ ] Unit tests for service layer
- [ ] Integration tests for repository
- [ ] Controller/API tests
- [ ] Manual verification steps

### Acceptance Criteria
<Copy from Azure DevOps task>
```

## Complexity Guidelines

| Feature Size | Approach |
|---|---|
| Small (1-3 files) | Implement directly |
| Medium (4-10 files) | Plan first, implement layer by layer |
| Large (10+ files) | Break into sub-tasks, implement incrementally |

## Error Recovery

| Situation | Action |
|-----------|--------|
| Requirements unclear | Comment on task asking for clarification |
| Conflicting requirements | Flag in task comment, implement safest interpretation |
| Requires architectural change | Document proposal in MR, request human review |
| Schema conflicts with other branches | Note in MR, coordinate with team |
