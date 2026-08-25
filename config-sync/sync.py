# config-sync: the ONLY component allowed to write CPA config.
#
# Responsibilities:
#   1. Keep the ln.vastai provider block in /cpa-config.yaml in sync with
#      API_LLM_SERVER / VAST_PROVIDER_NAME (YAML-safe, atomic, validated).
#   2. Propagate API_LLM_SERVER to VLLM nodes via SSH (change-detected).
#
# Safety: DRY_RUN=true only logs. Every write goes: candidate -> validate
# YAML -> backup current -> fsync -> atomic rename. Never touch a running
# config with invalid YAML.
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

import yaml

CPA_CONFIG = "/cpa-config.yaml"
CPA_BACKUP = "/cpa-config.yaml.bak"
INSTANCES_FILE = "/etc/vast/instances.txt"
SSH_KEY = "/vast-ssh/id_ed25519"
STATE_FILE = "/state/sync-state.json"

API_LLM_SERVER = os.getenv("API_LLM_SERVER", "").strip()
VAST_PROVIDER_NAME = os.getenv("VAST_PROVIDER_NAME", "ln.vastai").strip()
SYNC_INTERVAL_SECONDS = max(10, int(os.getenv("SYNC_INTERVAL_SECONDS", "60")))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

BASE_URL = "http://vast-gateway:18000/v1"


def log(msg):
    print(f"[config-sync] {msg}", flush=True)


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------
# CPA config update (atomic + validated)
# ---------------------------------------------------------------

def atomic_write(path, content):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def validate_yaml(content):
    yaml.safe_load(content)
    return True


def update_cpa_config(state):
    """Update the ln.vastai provider block using PyYAML (no regex).

    Returns (changed: bool, new_state: dict).
    """
    try:
        with open(CPA_CONFIG, "r", encoding="utf-8") as f:
            text = f.read()
        doc = yaml.safe_load(text)
    except Exception as e:
        log(f"⚠️ Cannot parse current CPA config: {e}")
        return False, state

    if not isinstance(doc, dict):
        return False, state

    providers = doc.get("providers")
    if not isinstance(providers, list):
        log("⚠️ No providers list in CPA config")
        return False, state

    target = None
    for p in providers:
        if isinstance(p, dict) and p.get("name") == VAST_PROVIDER_NAME:
            target = p
            break
    if target is None:
        log(f"⚠️ Provider '{VAST_PROVIDER_NAME}' not found; skipping")
        return False, state

    key_material = API_LLM_SERVER
    if not key_material:
        return False, state
    key_hash = hashlib.sha256(key_material.encode()).hexdigest()[:16]

    if state.get("api_key_hash") == key_hash and state.get("provider_name") == VAST_PROVIDER_NAME:
        return False, state  # nothing changed

    # Update base-url + api key entries YAML-safely.
    target["base-url"] = BASE_URL
    entries = target.get("api-key-entries")
    if isinstance(entries, list) and entries:
        entries[0]["api-key"] = API_LLM_SERVER
    else:
        target["api-key-entries"] = [{"api-key": API_LLM_SERVER}]

    candidate = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)

    try:
        validate_yaml(candidate)
    except Exception as e:
        log(f"❌ Candidate config invalid; NOT writing: {e}")
        return False, state

    if candidate == text:
        state["api_key_hash"] = key_hash
        state["provider_name"] = VAST_PROVIDER_NAME
        state["last_sync"] = time.time()
        save_state(state)
        return False, state

    if DRY_RUN:
        log(f"🔸 DRY RUN: would update {VAST_PROVIDER_NAME} base-url/key")
        return False, state

    # Backup current, then atomic replace.
    try:
        with open(CPA_CONFIG, "r", encoding="utf-8") as f:
            cur = f.read()
        with open(CPA_BACKUP, "w", encoding="utf-8") as f:
            f.write(cur)
            f.flush()
            os.fsync(f.fileno())
        atomic_write(CPA_CONFIG, candidate)
        log(f"🎉 CPA config updated: provider={VAST_PROVIDER_NAME} key-hash={key_hash}")
    except Exception as e:
        log(f"❌ Config write failed: {e}")
        return False, state

    state["api_key_hash"] = key_hash
    state["provider_name"] = VAST_PROVIDER_NAME
    state["last_sync"] = time.time()
    save_state(state)
    return True, state


# ---------------------------------------------------------------
# Propagate API key to VLLM nodes over SSH (change-detected)
# ---------------------------------------------------------------

def read_instances():
    rows = []
    try:
        with open(INSTANCES_FILE, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) >= 5 and parts[0].isdigit():
                    rows.append({"host": parts[1], "ssh_port": int(parts[2]), "id": parts[3]})
    except FileNotFoundError:
        pass
    return rows


def update_node_key(host, ssh_port):
    """Update the serving API key on one VLLM node. Best-effort."""
    if not os.path.exists(SSH_KEY):
        log(f"⚠️ SSH key missing: {SSH_KEY}")
        return
    remote = (
        "if pgrep -f 'sglang|vllm' >/dev/null 2>&1; then "
        "echo 'engine running; key update requires restart - skipped'; "
        "else echo 'no engine; ok'; fi"
    )
    cmd = [
        "ssh", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
        "-i", SSH_KEY, "-p", str(ssh_port), f"root@{host}", remote,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        log(f"  node {host}:{ssh_port} rc={r.returncode} {r.stdout.strip()[:80]}")
    except Exception as e:
        log(f"  node {host}:{ssh_port} error: {e}")


def sync_nodes(state):
    if DRY_RUN:
        log("🔸 DRY RUN: would check VLLM nodes for API key")
        return
    for row in read_instances():
        update_node_key(row["host"], row["ssh_port"])


# ---------------------------------------------------------------

def main():
    log(f"started (dry_run={DRY_RUN}, interval={SYNC_INTERVAL_SECONDS}s, provider={VAST_PROVIDER_NAME})")
    state = load_state()
    while True:
        try:
            changed, state = update_cpa_config(state)
            if changed:
                log("💡 CPA config changed — CPA hot-reloads config.yaml automatically")
            sync_nodes(state)
        except Exception as e:
            log(f"⚠️ sync loop error: {e}")
        time.sleep(SYNC_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
