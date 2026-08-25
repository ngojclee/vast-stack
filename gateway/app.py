import asyncio
import json
import os
import re
import time
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

INSTANCES_FILE = "/etc/vast/instances.txt"
BASE_LOCAL_PORT = 18001

VAST_API_KEY = os.getenv("VAST_API_KEY", "").strip()
API_LLM_SERVER = os.getenv("API_LLM_SERVER", "").strip()
PROVIDER_NAME = os.getenv("VAST_PROVIDER_NAME", "ln.vastai").strip()

VAST_INSTANCE_IDS = {
    x.strip()
    for x in os.getenv("VAST_INSTANCE_IDS", "").split(",")
    if x.strip()
}
VAST_CLUSTER_LABEL = os.getenv("VAST_CLUSTER_LABEL", "cpa-vllm").strip()
VAST_MANAGE_ALL_INSTANCES = (
    os.getenv("VAST_MANAGE_ALL_INSTANCES", "false").lower() == "true"
)
AUTO_SYNC_INSTANCES = (
    os.getenv("AUTO_SYNC_INSTANCES", "true").lower() == "true"
)
AUTO_SCALE_ENABLED = (
    os.getenv("AUTO_SCALE_ENABLED", "true").lower() == "true"
)

IDLE_TIMEOUT_MINUTES = max(
    1, int(os.getenv("IDLE_TIMEOUT_MINUTES", "30"))
)
BOOT_GRACE_SECONDS = max(
    30, int(os.getenv("BOOT_GRACE_SECONDS", "900"))
)
BOOT_POLL_SECONDS = max(
    2, int(os.getenv("BOOT_POLL_SECONDS", "5"))
)
SYNC_INTERVAL_SECONDS = max(
    10, int(os.getenv("SYNC_INTERVAL_SECONDS", "60"))
)
HEALTH_CACHE_SECONDS = max(
    0.0, float(os.getenv("HEALTH_CACHE_SECONDS", "3"))
)
REMOTE_LLM_PORT = int(os.getenv("REMOTE_LLM_PORT", "18000"))
LANGUAGE_PROMPT = os.getenv("LANGUAGE_PROMPT", "").strip()

VAST_LIST_URL = "https://console.vast.ai/api/v1/instances/"
VAST_MANAGE_URL = "https://console.vast.ai/api/v0/instances/{id}/"
VAST_HEADERS = (
    {"Authorization": f"Bearer {VAST_API_KEY}"}
    if VAST_API_KEY
    else {}
)

last_activity = time.time()
active_requests = 0
current_active_node = None
leader_lock = asyncio.Lock()
health_cache = {}
last_no_scope_warning = 0.0


def parse_instance_file():
    rows = []
    try:
        with open(INSTANCES_FILE, "r", encoding="utf-8") as f:
            raw_lines = f.readlines()
    except FileNotFoundError:
        return rows

    legacy_index = 0
    for raw in raw_lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        parts = line.split()
        try:
            if len(parts) >= 5 and parts[0].isdigit():
                local_port = int(parts[0])
                host = parts[1]
                ssh_port = int(parts[2])
                instance_id = str(parts[3])
                price = float(parts[4])
            elif len(parts) >= 2:
                local_port = BASE_LOCAL_PORT + legacy_index
                legacy_index += 1
                host = parts[0]
                ssh_port = int(parts[1])
                instance_id = (
                    str(parts[2]) if len(parts) >= 3 else None
                )
                price = (
                    float(parts[3]) if len(parts) >= 4 else 999.0
                )
            else:
                continue
        except (TypeError, ValueError):
            print(
                f"⚠️ Ignoring invalid instances.txt line: {line}",
                flush=True,
            )
            continue

        rows.append({
            "local_port": local_port,
            "host": host,
            "ssh_port": ssh_port,
            "instance_id": instance_id,
            "price": price,
        })

    return rows


def get_cluster_nodes():
    nodes = []
    for row in parse_instance_file():
        nodes.append({
            **row,
            "target": f"vast-tunnel:{row['local_port']}",
        })

    nodes.sort(key=lambda x: (x["price"], x["local_port"]))
    return nodes


def existing_port_map():
    mapping = {}
    used = set()

    for row in parse_instance_file():
        local_port = row["local_port"]
        instance_id = row.get("instance_id")

        if instance_id:
            mapping[str(instance_id)] = local_port
        used.add(local_port)

    return mapping, used


def next_free_port(used):
    port = BASE_LOCAL_PORT
    while port in used:
        port += 1
        if port > 65535:
            raise RuntimeError("No free local tunnel ports")
    return port


def instance_price(inst):
    for value in (
        inst.get("dph_total"),
        inst.get("dph_base"),
        (inst.get("search") or {}).get("totalHour"),
        (inst.get("search") or {}).get("dph"),
    ):
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            pass
    return 999.0


def instance_ssh_endpoint(inst):
    public_ip = inst.get("public_ipaddr")
    ports = inst.get("ports") or {}

    if public_ip and isinstance(ports, dict):
        p22 = ports.get("22/tcp")
        if (
            isinstance(p22, list)
            and p22
            and isinstance(p22[0], dict)
            and p22[0].get("HostPort")
        ):
            return str(public_ip), int(p22[0]["HostPort"])

    ssh_host = inst.get("ssh_host")
    ssh_port = inst.get("ssh_port")
    if ssh_host and ssh_port:
        return str(ssh_host), int(ssh_port)

    return None, None


def seed_instance_ids():
    return {
        str(row.get("instance_id"))
        for row in parse_instance_file()
        if row.get("instance_id")
    }


def instance_is_in_cluster(inst, seed_ids):
    inst_id = str(inst.get("id", ""))

    # Explicit allowlist is the strictest mode.
    if VAST_INSTANCE_IDS:
        return inst_id in VAST_INSTANCE_IDS

    # Opt-in only: use this if the Vast account is dedicated to
    # this cluster and every instance may be managed by the gateway.
    if VAST_MANAGE_ALL_INSTANCES:
        return True

    # Migration-safe default:
    # - keep IDs already present in instances.txt
    # - auto-discover newly created instances with the cluster label
    if inst_id in seed_ids:
        return True

    if VAST_CLUSTER_LABEL:
        return str(inst.get("label") or "") == VAST_CLUSTER_LABEL

    return False


async def fetch_vast_instances():
    if not VAST_API_KEY:
        return []

    all_instances = []
    after_token = None
    seed_ids = seed_instance_ids()

    while True:
        params = {"limit": 25}

        # Server-side label filtering is safe only when there are no
        # legacy/seed IDs to preserve and no broader scope requested.
        if (
            not VAST_INSTANCE_IDS
            and not VAST_MANAGE_ALL_INSTANCES
            and not seed_ids
            and VAST_CLUSTER_LABEL
        ):
            params["select_filters"] = json.dumps({
                "label": {"eq": VAST_CLUSTER_LABEL}
            })

        if after_token:
            params["after_token"] = after_token

        resp = await app.state.control_http.get(
            VAST_LIST_URL,
            headers=VAST_HEADERS,
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()

        page = data.get("instances", [])
        if isinstance(page, list):
            all_instances.extend(page)

        after_token = data.get("next_token")
        if not after_token:
            break

    return [
        inst for inst in all_instances
        if instance_is_in_cluster(inst, seed_ids)
    ]


async def auto_sync_instances_from_vast():
    global last_no_scope_warning

    if not AUTO_SYNC_INSTANCES or not VAST_API_KEY:
        return []

    if (
        not VAST_INSTANCE_IDS
        and not VAST_MANAGE_ALL_INSTANCES
        and not VAST_CLUSTER_LABEL
        and not seed_instance_ids()
    ):
        now = time.time()
        if now - last_no_scope_warning > 300:
            print(
                "🛑 Auto-sync disabled for safety: set "
                "VAST_CLUSTER_LABEL / VAST_INSTANCE_IDS, or seed "
                "instances.txt first",
                flush=True,
            )
            last_no_scope_warning = now
        return []

    try:
        instances = await fetch_vast_instances()
    except Exception as e:
        print(f"⚠️ Vast instance sync failed: {e}", flush=True)
        return []

    port_map, used_ports = existing_port_map()
    rows = []

    for inst in instances:
        inst_id = inst.get("id")
        if inst_id is None:
            continue

        inst_id = str(inst_id)
        host, ssh_port = instance_ssh_endpoint(inst)
        if not host or not ssh_port:
            # Stopped/provisioning instances may temporarily have no
            # usable SSH endpoint. Preserve any old row so a restart
            # can recover when the endpoint returns.
            old = next(
                (
                    row for row in parse_instance_file()
                    if str(row.get("instance_id")) == inst_id
                ),
                None,
            )
            if old:
                rows.append({
                    **old,
                    "price": instance_price(inst),
                })
            continue

        local_port = port_map.get(inst_id)
        if local_port is None:
            local_port = next_free_port(used_ports)
            port_map[inst_id] = local_port
            used_ports.add(local_port)

        rows.append({
            "local_port": local_port,
            "host": host,
            "ssh_port": ssh_port,
            "instance_id": inst_id,
            "price": instance_price(inst),
        })

    # Price ordering chooses the leader; local_port remains stable.
    rows.sort(key=lambda x: (x["price"], x["local_port"]))

    header = (
        "# Managed by vast-gateway. Do not reorder local ports manually.\n"
        "# FORMAT: LOCAL_PORT SSH_HOST SSH_PORT INSTANCE_ID PRICE\n"
    )
    body = "".join(
        f"{row['local_port']} {row['host']} {row['ssh_port']} "
        f"{row['instance_id']} {row['price']:.6f}\n"
        for row in rows
    )
    new_content = header + body

    try:
        old_content = ""
        if os.path.exists(INSTANCES_FILE):
            with open(INSTANCES_FILE, "r", encoding="utf-8") as f:
                old_content = f.read()

        if old_content != new_content:
            # INSTANCES_FILE is a direct bind-mounted file. Write it
            # in place; os.replace() cannot replace a mount point.
            with open(INSTANCES_FILE, "w", encoding="utf-8") as f:
                f.write(new_content)
                f.flush()
                os.fsync(f.fileno())
            print(
                f"🔄 Synced {len(rows)} scoped Vast instance(s)",
                flush=True,
            )
    except Exception as e:
        print(f"⚠️ Could not write {INSTANCES_FILE}: {e}", flush=True)

    return instances


async def is_node_healthy(target, force=False):
    now = time.time()
    cached = health_cache.get(target)

    if (
        not force
        and cached
        and now - cached[0] <= HEALTH_CACHE_SECONDS
    ):
        return cached[1]

    ok = False
    try:
        health_timeout = httpx.Timeout(
            connect=5.0,
            read=8.0,
            write=5.0,
            pool=5.0,
        )
        r = await app.state.control_http.get(
            f"http://{target}/health",
            timeout=health_timeout,
        )

        if r.status_code == 200:
            ok = True
        else:
            headers = (
                {"Authorization": f"Bearer {API_LLM_SERVER}"}
                if API_LLM_SERVER
                else {}
            )
            r2 = await app.state.control_http.get(
                f"http://{target}/v1/models",
                headers=headers,
                timeout=health_timeout,
            )
            ok = r2.status_code in (200, 401)
    except Exception:
        ok = False

    health_cache[target] = (now, ok)
    return ok


async def set_instance_state(inst_id, state):
    if not VAST_API_KEY or not inst_id:
        return False

    if state not in ("running", "stopped"):
        raise ValueError(f"Unsupported Vast state: {state}")

    url = VAST_MANAGE_URL.format(id=inst_id)

    try:
        r = await app.state.control_http.put(
            url,
            headers=VAST_HEADERS,
            json={"state": state},
        )
        if r.status_code == 200:
            return True

        print(
            f"⚠️ Vast state change failed id={inst_id} "
            f"state={state} status={r.status_code}: "
            f"{r.text[:300]}",
            flush=True,
        )
    except Exception as e:
        print(
            f"⚠️ Vast state change error id={inst_id}: {e}",
            flush=True,
        )

    return False


async def find_healthy_node(nodes, start_index=0):
    for node in nodes[start_index:]:
        if await is_node_healthy(node["target"]):
            return node
    return None


async def resolve_active_node():
    global current_active_node

    nodes = get_cluster_nodes()
    if not nodes:
        return None

    async with leader_lock:
        # Re-read after waiting for the lock because instances.txt may
        # have been updated while another request was resolving.
        nodes = get_cluster_nodes()
        if not nodes:
            current_active_node = None
            return None

        primary = nodes[0]

        if await is_node_healthy(primary["target"]):
            if (
                not current_active_node
                or current_active_node["target"]
                != primary["target"]
            ):
                print(
                    f"🎯 Leader: {primary['target']} "
                    f"(instance={primary['instance_id']}, "
                    f"USD {primary['price']:.6f}/hr)",
                    flush=True,
                )
            current_active_node = primary
            return primary

        # Strict primary-only mode when autoscale is disabled.
        if not AUTO_SCALE_ENABLED:
            current_active_node = None
            return None

        fallback = await find_healthy_node(nodes, start_index=1)
        if fallback:
            if (
                not current_active_node
                or current_active_node["target"]
                != fallback["target"]
            ):
                print(
                    f"⚠️ Primary unavailable; fallback: "
                    f"{fallback['target']} "
                    f"(instance={fallback['instance_id']}, "
                    f"USD {fallback['price']:.6f}/hr)",
                    flush=True,
                )
            current_active_node = fallback
            return fallback

        inst_id = primary.get("instance_id")
        if not inst_id:
            current_active_node = None
            return None

        print(
            f"🚀 Cold-starting instance {inst_id} "
            f"(USD {primary['price']:.6f}/hr)",
            flush=True,
        )

        started = await set_instance_state(inst_id, "running")
        if not started:
            current_active_node = None
            return None

        deadline = time.time() + BOOT_GRACE_SECONDS
        last_sync = 0.0

        while time.time() < deadline:
            await asyncio.sleep(BOOT_POLL_SECONDS)

            # Refresh SSH endpoint while the instance boots.
            if time.time() - last_sync >= 15:
                await auto_sync_instances_from_vast()
                last_sync = time.time()

            fresh_nodes = get_cluster_nodes()
            fresh_primary = next(
                (
                    n for n in fresh_nodes
                    if str(n.get("instance_id")) == str(inst_id)
                ),
                None,
            )
            if fresh_primary and await is_node_healthy(
                fresh_primary["target"],
                force=True,
            ):
                current_active_node = fresh_primary
                print(
                    f"🎉 Instance {inst_id} is LIVE",
                    flush=True,
                )
                return fresh_primary

        print(
            f"❌ Instance {inst_id} did not become healthy within "
            f"{BOOT_GRACE_SECONDS}s",
            flush=True,
        )
        current_active_node = None
        return None


async def choose_confirmed_leader(nodes):
    global current_active_node

    # nodes are already sorted by cost. Always prefer the cheapest
    # confirmed-healthy node instead of pinning an older fallback.
    leader = await find_healthy_node(nodes)
    if leader:
        current_active_node = leader
    return leader


async def scaler_loop():
    global current_active_node

    while True:
        try:
            await asyncio.sleep(SYNC_INTERVAL_SECONDS)

            scoped_instances = await auto_sync_instances_from_vast()

            if not AUTO_SCALE_ENABLED:
                continue

            if active_requests > 0:
                continue

            nodes = get_cluster_nodes()
            if not nodes:
                current_active_node = None
                continue

            idle_seconds = time.time() - last_activity

            # Stop all scoped instances after the idle timeout.
            if idle_seconds > IDLE_TIMEOUT_MINUTES * 60:
                status_by_id = {
                    str(inst.get("id")): inst
                    for inst in scoped_instances
                    if inst.get("id") is not None
                }

                for node in nodes:
                    inst_id = str(node.get("instance_id") or "")
                    inst = status_by_id.get(inst_id)
                    if not inst_id:
                        continue

                    intended = (
                        (inst or {}).get("intended_status")
                        or (inst or {}).get("actual_status")
                    )
                    if intended == "stopped":
                        continue

                    if await set_instance_state(
                        inst_id, "stopped"
                    ):
                        print(
                            f"💤 Idle>{IDLE_TIMEOUT_MINUTES}m; "
                            f"stopped {inst_id}",
                            flush=True,
                        )

                current_active_node = None
                continue

            # Before idle timeout, consolidate only if a healthy leader
            # is confirmed. Never stop peers without a healthy leader.
            leader = await choose_confirmed_leader(nodes)
            if not leader or not leader.get("instance_id"):
                continue

            leader_id = str(leader["instance_id"])
            status_by_id = {
                str(inst.get("id")): inst
                for inst in scoped_instances
                if inst.get("id") is not None
            }

            leader_pos = next(
                (
                    i for i, n in enumerate(nodes)
                    if str(n.get("instance_id") or "") == leader_id
                ),
                0,
            )

            # Leave cheaper-but-not-yet-healthy nodes alone so they
            # can finish booting/recover. Stop only more expensive
            # redundant nodes after a healthy leader is confirmed.
            for node in nodes[leader_pos + 1:]:
                inst_id = str(node.get("instance_id") or "")
                if not inst_id:
                    continue

                inst = status_by_id.get(inst_id)
                actual = (
                    (inst or {}).get("actual_status")
                    or (inst or {}).get("intended_status")
                )
                if actual != "running":
                    continue

                if await set_instance_state(
                    inst_id, "stopped"
                ):
                    print(
                        f"💰 Stopped redundant instance {inst_id}; "
                        f"leader={leader_id}",
                        flush=True,
                    )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"⚠️ scaler_loop error: {e}", flush=True)


def inject_language_prompt(body):
    if not body or not LANGUAGE_PROMPT:
        return body

    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return body

    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return body

    first = messages[0]
    if (
        isinstance(first, dict)
        and first.get("role") == "system"
        and isinstance(first.get("content"), str)
    ):
        if LANGUAGE_PROMPT not in first["content"]:
            first["content"] += "\n\n" + LANGUAGE_PROMPT
    else:
        messages.insert(
            0,
            {"role": "system", "content": LANGUAGE_PROMPT},
        )

    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def filtered_request_headers(req):
    hop_by_hop = {
        "host",
        "content-length",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "accept-encoding",
    }
    return {
        k: v
        for k, v in req.headers.items()
        if k.lower() not in hop_by_hop
    }


def filtered_response_headers(headers):
    hop_by_hop = {
        "content-length",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
    return {
        k: v
        for k, v in headers.items()
        if k.lower() not in hop_by_hop
    }


@asynccontextmanager
async def lifespan(app_obj):
    control_timeout = httpx.Timeout(
        connect=10.0,
        read=20.0,
        write=10.0,
        pool=10.0,
    )
    proxy_timeout = httpx.Timeout(
        connect=10.0,
        read=900.0,
        write=120.0,
        pool=10.0,
    )

    app_obj.state.control_http = httpx.AsyncClient(
        timeout=control_timeout
    )
    app_obj.state.proxy_http = httpx.AsyncClient(
        timeout=proxy_timeout,
        limits=httpx.Limits(
            max_connections=200,
            max_keepalive_connections=50,
        ),
    )

    await auto_sync_instances_from_vast()

    scaler_task = asyncio.create_task(scaler_loop())
    try:
        yield
    finally:
        scaler_task.cancel()
        await asyncio.gather(
            scaler_task,
            return_exceptions=True,
        )
        await app_obj.state.control_http.aclose()
        await app_obj.state.proxy_http.aclose()


app = FastAPI(lifespan=lifespan)


@app.get("/__gateway/health")
async def gateway_health():
    return {"ok": True}


@app.get("/__gateway/status")
async def gateway_status():
    nodes = get_cluster_nodes()
    return {
        "ok": True,
        "auto_scale_enabled": AUTO_SCALE_ENABLED,
        "auto_sync_instances": AUTO_SYNC_INSTANCES,
        "active_requests": active_requests,
        "idle_seconds": round(time.time() - last_activity, 1),
        "active_instance_id": (
            current_active_node.get("instance_id")
            if current_active_node
            else None
        ),
        "nodes": [
            {
                "local_port": n["local_port"],
                "instance_id": n["instance_id"],
                "price": n["price"],
            }
            for n in nodes
        ],
    }


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE",
             "OPTIONS", "HEAD"],
)
async def proxy(req: Request, path: str):
    global last_activity, active_requests

    last_activity = time.time()
    active_requests += 1

    is_stream = False
    stream_request_active = False
    upstream_resp = None

    try:
        node = await resolve_active_node()
        if not node:
            return JSONResponse(
                {"error": "No healthy active instance"},
                status_code=503,
            )

        target_url = f"http://{node['target']}/{path}"
        if req.url.query:
            target_url += f"?{req.url.query}"

        body = await req.body()
        headers = filtered_request_headers(req)

        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
            is_stream = bool(
                isinstance(payload, dict)
                and payload.get("stream", False)
            )
        except Exception:
            is_stream = False

        if req.method == "POST":
            body = inject_language_prompt(body)

        if is_stream:
            upstream_req = app.state.proxy_http.build_request(
                method=req.method,
                url=target_url,
                headers=headers,
                content=body,
            )
            upstream_resp = await app.state.proxy_http.send(
                upstream_req,
                stream=True,
            )

            if upstream_resp.status_code >= 400:
                error_body = await upstream_resp.aread()
                response_headers = filtered_response_headers(
                    upstream_resp.headers
                )
                status = upstream_resp.status_code
                await upstream_resp.aclose()
                upstream_resp = None
                return Response(
                    content=error_body,
                    status_code=status,
                    headers=response_headers,
                )

            stream_request_active = True

            async def sse_generator():
                nonlocal upstream_resp
                pending_whitespace_chunks = []

                try:
                    async for line in upstream_resp.aiter_lines():
                        if not line:
                            continue

                        if line == "data: [DONE]":
                            for pending in pending_whitespace_chunks:
                                yield (
                                    "data: "
                                    + json.dumps(
                                        pending,
                                        ensure_ascii=False,
                                    )
                                    + "\n\n"
                                ).encode("utf-8")
                            pending_whitespace_chunks.clear()
                            yield b"data: [DONE]\n\n"
                            continue

                        if line.startswith("data: "):
                            try:
                                data = json.loads(line[6:])
                                choices = data.get("choices", [])

                                if choices:
                                    delta = (
                                        choices[0].get("delta", {})
                                    )
                                    content = delta.get("content")
                                    tool_calls = delta.get(
                                        "tool_calls"
                                    )
                                    finish_reason = choices[0].get(
                                        "finish_reason"
                                    )

                                    if (
                                        tool_calls
                                        or finish_reason
                                        == "tool_calls"
                                    ):
                                        pending_whitespace_chunks.clear()
                                        if (
                                            isinstance(content, str)
                                            and content.strip() == ""
                                        ):
                                            delta.pop(
                                                "content", None
                                            )
                                        yield (
                                            "data: "
                                            + json.dumps(
                                                data,
                                                ensure_ascii=False,
                                            )
                                            + "\n\n"
                                        ).encode("utf-8")
                                        continue

                                    if (
                                        isinstance(content, str)
                                        and content.strip() == ""
                                    ):
                                        if (
                                            not delta.get("role")
                                            and not delta.get(
                                                "reasoning_content"
                                            )
                                        ):
                                            pending_whitespace_chunks.append(
                                                data
                                            )
                                            continue

                                        delta.pop("content", None)
                                        yield (
                                            "data: "
                                            + json.dumps(
                                                data,
                                                ensure_ascii=False,
                                            )
                                            + "\n\n"
                                        ).encode("utf-8")
                                        continue

                                    if (
                                        isinstance(content, str)
                                        and content.strip() != ""
                                    ):
                                        for pending in (
                                            pending_whitespace_chunks
                                        ):
                                            yield (
                                                "data: "
                                                + json.dumps(
                                                    pending,
                                                    ensure_ascii=False,
                                                )
                                                + "\n\n"
                                            ).encode("utf-8")
                                        pending_whitespace_chunks.clear()
                                        yield (
                                            "data: "
                                            + json.dumps(
                                                data,
                                                ensure_ascii=False,
                                            )
                                            + "\n\n"
                                        ).encode("utf-8")
                                        continue

                                    yield (
                                        "data: "
                                        + json.dumps(
                                            data,
                                            ensure_ascii=False,
                                        )
                                        + "\n\n"
                                    ).encode("utf-8")
                                    continue
                            except Exception:
                                pass

                        yield (line + "\n\n").encode("utf-8")

                finally:
                    global active_requests

                    active_requests = max(
                        0, active_requests - 1
                    )
                    if upstream_resp is not None:
                        await upstream_resp.aclose()
                        upstream_resp = None

            return StreamingResponse(
                sse_generator(),
                status_code=upstream_resp.status_code,
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        upstream = await app.state.proxy_http.request(
            method=req.method,
            url=target_url,
            headers=headers,
            content=body,
        )

        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=filtered_response_headers(upstream.headers),
        )

    except Exception as e:
        print(f"⚠️ Proxy error: {e}", flush=True)
        return JSONResponse(
            {
                "error":
                f"Failed communicating with active instance: {e}"
            },
            status_code=503,
        )

    finally:
        # Streaming decrements when the generator actually finishes.
        if not (is_stream and stream_request_active):
            active_requests = max(0, active_requests - 1)

        # If an exception happens after opening a stream but before
        # StreamingResponse takes ownership, close the response.
        if (
            upstream_resp is not None
            and not stream_request_active
        ):
            await upstream_resp.aclose()
