"""Google Sheets API v4 — stdlib only, auth via google_oauth.

    from agent_skill_toolkit.integrations import gsheets
    gsheets.sheet_title(SPREADSHEET_ID, gid)          # gid -> tab title (A1 needs the title)
    gsheets.read(SPREADSHEET_ID, "Sheet1!A1:Q")       # list[list[str]]
    gsheets.update(SPREADSHEET_ID, "Sheet1!B153", [["Prod", ...]])
    gsheets.append(SPREADSHEET_ID, "Sheet1!A:Q", [[...]])
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from ..core import env, tls
from . import google_oauth, google_sa

BASE = "https://sheets.googleapis.com/v4/spreadsheets"


def _token() -> str:
    """Prefer a service account (durable, unattended) when a key file is set; else OAuth."""
    if env.get("GOOGLE_SA_KEY_FILE"):
        return google_sa.access_token()
    return google_oauth.access_token()


def _call(method: str, url: str, body: dict = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + _token(),
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30, context=tls.context()) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"sheets {method} {e.code}: {e.read().decode(errors='replace')}") from None


def metadata(spreadsheet_id: str) -> dict:
    return _call("GET", f"{BASE}/{spreadsheet_id}?fields=sheets.properties,properties.title")


def sheet_title(spreadsheet_id: str, gid) -> str:
    """Map a numeric gid (sheetId) to its tab title; A1 ranges need the title, not the gid."""
    gid = int(gid)
    for s in metadata(spreadsheet_id).get("sheets", []):
        p = s.get("properties", {})
        if p.get("sheetId") == gid:
            return p.get("title", "")
    raise RuntimeError(f"no sheet with gid {gid}")


def read(spreadsheet_id: str, a1: str) -> list:
    rng = urllib.parse.quote(a1, safe="")
    return _call("GET", f"{BASE}/{spreadsheet_id}/values/{rng}").get("values", [])


def update(spreadsheet_id: str, a1: str, values: list) -> dict:
    """Overwrite the cells at a1 (RAW — values stored verbatim, no formula/date parsing)."""
    rng = urllib.parse.quote(a1, safe="")
    url = f"{BASE}/{spreadsheet_id}/values/{rng}?valueInputOption=USER_ENTERED"
    return _call("PUT", url, {"range": a1, "majorDimension": "ROWS", "values": values})


def append(spreadsheet_id: str, a1: str, values: list) -> dict:
    """Append rows after the last row of the a1 table range."""
    rng = urllib.parse.quote(a1, safe="")
    url = (f"{BASE}/{spreadsheet_id}/values/{rng}:append"
           "?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS")
    return _call("POST", url, {"range": a1, "majorDimension": "ROWS", "values": values})
