# Tester Notification Template

Use this template to compose the notification message posted to the Azure DevOps
work item when the implementation is ready for testing.

---

## Notification Format

```html
<b>[AI Agent] Ready for Testing</b><br/>
<br/>
<b>Task:</b> #<task_id> - <task_title><br/>
<b>Branch:</b> <code><branch_name></code><br/>
<b>Merge Request:</b> <a href="<mr_url>">!<mr_iid> - <mr_title></a><br/>
<b>Dev Environment:</b> <a href="<dev_url>"><dev_url></a><br/>
<br/>
<b>Changes Summary:</b><br/>
<ul>
<li><brief description of change 1></li>
<li><brief description of change 2></li>
</ul>
<br/>
<b>Test Scenarios:</b><br/>
<ol>
<li><test scenario derived from acceptance criteria 1></li>
<li><test scenario derived from acceptance criteria 2></li>
<li><test scenario derived from acceptance criteria 3></li>
</ol>
<br/>
<b>Acceptance Criteria:</b><br/>
<ol>
<li><acceptance criterion 1></li>
<li><acceptance criterion 2></li>
</ol>
<br/>
<b>Notes:</b><br/>
<ul>
<li><any special configuration or setup needed></li>
<li><any known limitations></li>
</ul>
```

## Guidelines for Filling the Template

1. **Changes Summary:** Keep it concise and non-technical. Testers need to know
   WHAT changed from a user perspective, not HOW the code was modified.

2. **Test Scenarios:** Derive from the acceptance criteria. Each scenario should be
   a concrete action the tester can perform:
   - "Login with valid credentials and verify the dashboard loads"
   - "Submit the form with empty required fields and verify error messages"

3. **Dev Environment URL:** Include the specific page/endpoint to test, not just
   the base URL. For example: `https://dev.example.com/users/settings` instead of
   `https://dev.example.com`.

4. **Notes:** Include anything the tester needs to know:
   - Test data requirements ("Use user test@example.com / password123")
   - Feature flags to enable
   - Browser/device requirements
   - Known limitations or out-of-scope items
