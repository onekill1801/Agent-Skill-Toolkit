# Headless QR Login (best-effort)

> ⚠️ **Unverified bodies.** The `/auth/qr/*` endpoint *paths* are confirmed from
> traffic, but their request/response field names were NOT captured. This is a
> scaffold — expect to adjust `auth.py` after one body capture. For normal use,
> prefer **Token paste** below.

## Recommended: Token paste (reliable)
1. Log in to the chat web app (`FCHAT_WEB_ORIGIN`) in a browser.
2. F12 → **Network** → click any request to the chat API host (`FCHAT_BASE_API_URL`).
3. Copy from **Request Headers**:
   - `authorization: Bearer <JWT>` → put the JWT into `FCHAT_BEARER_TOKEN`
   - `x-app: <value>` → put into `FCHAT_X_APP`
4. Verify:
   ```
   cd .claude/skills/tchat-automation/tools
   python auth.py whoami
   ```

## Experimental: QR flow
```
python auth.py qr-generate            # -> QR token (render/scan with TChat mobile app)
python auth.py qr-scan <qr_token>     # poll until scanned
python auth.py qr-confirm <qr_token>  # poll until confirmed -> session token
```
If any step errors, capture the real payload (DevTools → Network → the
`/auth/qr/*` request → **Payload** and **Response**) and fix the field names in
`auth.py` (currently guessed as `{"token": ...}`).

## Token expiry
JWT is short-lived. On `401`, repeat **Token paste**. A client-side refresh
mechanism exists but its endpoint wasn't captured.
