# ==============================================================================
# Windmill Flow: git_backflow_valve (Signed State Persistence to Git)
# ==============================================================================
# Triggered when an operator or script alters a tenant policy in runtime.
# Serializes state, computes HMAC-SHA256 signature, and writes upstream to GitHub.
# Downstream CI/CD verifies the signature and auto-approves with a graceful exit.

import os
import hmac
import hashlib
import json
import yaml
import requests
import base64

GITHUB_REPO = os.environ.get("GITHUB_REPO", "org/observability-rules")
GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}"
GITHUB_TOKEN = os.environ.get("GITHUB_BACKFLOW_TOKEN", "")
BACKFLOW_SECRET = os.environ.get("BACKFLOW_SHARED_SECRET", "change-me-backflow-secret")

def main(tenant_id: str, policy_payload: dict, operator: str = "system"):
    # 1. Deterministic YAML serialization
    yaml_content = yaml.dump(policy_payload, sort_keys=True)
    file_path = f"rules/tenants/{tenant_id}.yaml"

    # 2. Cryptographic digest & HMAC signature
    content_hash = hashlib.sha256(yaml_content.encode("utf-8")).hexdigest()
    signature = hmac.new(
        BACKFLOW_SECRET.encode("utf-8"),
        content_hash.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    # 3. Format structured audit commit
    commit_msg = (
        f"chore(rules): update tenant policy for {tenant_id} by {operator}

"
        f"[source:windmill-fastpath]
"
        f"[tenant:{tenant_id}]
"
        f"[state-hash:{content_hash}]
"
        f"[signature:{signature}]"
    )

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }

    # 4. Retrieve existing blob SHA if updating existing file
    r = requests.get(f"{GITHUB_API}/contents/{file_path}", headers=headers, timeout=10)
    sha = r.json().get("sha") if r.status_code == 200 else None

    # 5. Push commit to Git upstream
    payload = {
        "message": commit_msg,
        "content": base64.b64encode(yaml_content.encode("utf-8")).decode("utf-8"),
        "branch": "main"
    }
    if sha:
        payload["sha"] = sha

    push_res = requests.put(f"{GITHUB_API}/contents/{file_path}", headers=headers, json=payload, timeout=10)
    push_res.raise_for_status()

    return {
        "status": "reconciled_upstream",
        "tenant_id": tenant_id,
        "state_hash": content_hash,
        "signature": signature,
        "commit_sha": push_res.json().get("commit", {}).get("sha")
    }
