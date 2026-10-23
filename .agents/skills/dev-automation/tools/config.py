#!/usr/bin/env python3
"""Configuration loader for dev-automation tools.

Reads settings from environment variables or a .env file in the project root.
Zero external dependencies - uses only Python stdlib.
"""

import os
import re
import sys

# Cross-platform: force UTF-8 stdout/stderr so JSON output with non-ASCII (Vietnamese
# task names, comments) doesn't crash on a Windows cp1252/cp437 console. Runs once on
# import; every dev-automation tool (and atask tools via client.py) imports config.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

_ENV_LOADED = False


def _strip_inline_comment(value: str) -> str:
    """Remove trailing # comment from .env values (not inside quotes)."""
    value = value.strip()
    if not value or value[0] in "\"'":
        return value.strip("\"'")
    hash_at = value.find("#")
    if hash_at >= 0:
        value = value[:hash_at]
    return value.strip()


def _load_dotenv():
    """Parse .env file and inject into os.environ (only once)."""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True

    env_path = os.path.join(os.getcwd(), ".env")
    if not os.path.isfile(env_path):
        search = os.path.dirname(os.path.abspath(__file__))
        for _ in range(5):
            candidate = os.path.join(search, ".env")
            if os.path.isfile(candidate):
                env_path = candidate
                break
            search = os.path.dirname(search)
        else:
            return

    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.match(r'^([A-Za-z_]\w*)=(.*)$', line)
            if match:
                key = match.group(1)
                value = _strip_inline_comment(match.group(2))
                os.environ.setdefault(key, value)


def get(key: str, default: str = "") -> str:
    """Get a config value from environment, loading .env if needed."""
    _load_dotenv()
    return os.environ.get(key, default).strip()


# Azure DevOps
AZURE_DEVOPS_ORG = property(lambda _: get("AZURE_DEVOPS_ORG"))
AZURE_DEVOPS_PROJECT = property(lambda _: get("AZURE_DEVOPS_PROJECT"))
AZURE_DEVOPS_PAT = property(lambda _: get("AZURE_DEVOPS_PAT"))

# GitLab
GITLAB_URL = property(lambda _: get("GITLAB_URL"))
GITLAB_PRIVATE_TOKEN = property(lambda _: get("GITLAB_PRIVATE_TOKEN"))
GITLAB_PROJECT_ID = property(lambda _: get("GITLAB_PROJECT_ID"))


def azure_base_url() -> str:
    org = get("AZURE_DEVOPS_ORG")
    project = get("AZURE_DEVOPS_PROJECT")
    return f"https://dev.azure.com/{org}/{project}/_apis"


def gitlab_base_url() -> str:
    url = get("GITLAB_URL", "https://gitlab.com").rstrip("/")
    return f"{url}/api/v4"


def validate() -> list[str]:
    """Return a list of missing required config keys."""
    required = [
        "AZURE_DEVOPS_ORG", "AZURE_DEVOPS_PROJECT", "AZURE_DEVOPS_PAT",
        "GITLAB_URL", "GITLAB_PRIVATE_TOKEN", "GITLAB_PROJECT_ID",
    ]
    return [k for k in required if not get(k)]


if __name__ == "__main__":
    missing = validate()
    if missing:
        print(f"Missing config: {', '.join(missing)}")
    else:
        print("All config keys present.")
        print(f"  Azure: {azure_base_url()}")
        print(f"  GitLab: {gitlab_base_url()}")
