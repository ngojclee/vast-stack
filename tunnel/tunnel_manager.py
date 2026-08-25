# Dynamic SSH tunnel manager for Vast.ai cluster.
#
# Reads /etc/vast/instances.txt (LOCAL_PORT SSH_HOST SSH_PORT INSTANCE_ID PRICE)
# and maintains one SSH local-forward per row: 0.0.0.0:LOCAL_PORT ->
# node:REMOTE_LLM_PORT. The SSH private key is mounted at /vast-ssh/id_ed25519.
import asyncio
import os
import signal

INSTANCES_FILE = os.getenv("INSTANCES_FILE", "/etc/vast/instances.txt")
SSH_KEY = os.getenv("SSH_KEY", "/vast-ssh/id_ed25519")
KNOWN_HOSTS = os.getenv("KNOWN_HOSTS", "/vast-ssh/known_hosts")
SCAN_SECONDS = max(2, int(os.getenv("TUNNEL_SCAN_SECONDS", "5")))
REMOTE_LLM_PORT = int(os.getenv("REMOTE_LLM_PORT", "18000"))
BASE_LOCAL_PORT = 18001

tasks = {}
stopping = False


def parse_instances():
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
                instance_id = str(parts[2]) if len(parts) >= 3 else f"legacy-{local_port}"
                price = float(parts[3]) if len(parts) >= 4 else 999.0
            else:
                continue
        except (TypeError, ValueError):
            print(f"⚠️ Ignoring invalid instances.txt line: {line}", flush=True)
            continue

        if not (1 <= local_port <= 65535 and 1 <= ssh_port <= 65535):
            print(f"⚠️ Ignoring invalid port mapping: {line}", flush=True)
            continue

        rows.append({
            "local_port": local_port,
            "host": host,
            "ssh_port": ssh_port,
            "instance_id": instance_id,
            "price": price,
        })

    # One tunnel per local port. First valid line wins.
    unique = {}
    for row in rows:
        unique.setdefault(row["local_port"], row)
    return list(unique.values())


async def stop_process(proc):
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=5)
    except Exception:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.wait()
        except Exception:
            pass


async def tunnel_worker(spec):
    local_port = spec["local_port"]
    host = spec["host"]
    ssh_port = spec["ssh_port"]
    instance_id = spec["instance_id"]

    if not os.path.exists(SSH_KEY):
        print(f"❌ Missing SSH key: {SSH_KEY}", flush=True)
        while True:
            await asyncio.sleep(30)

    while True:
        cmd = [
            "ssh",
            "-q",
            "-N",
            "-g",
            "-L", f"0.0.0.0:{local_port}:127.0.0.1:{REMOTE_LLM_PORT}",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
            "-i", SSH_KEY,
            "-p", str(ssh_port),
            f"root@{host}",
        ]

        print(
            f"🔗 Tunnel {local_port} -> {host}:{ssh_port} (instance={instance_id})",
            flush=True,
        )

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(*cmd)
            rc = await proc.wait()
            print(f"⚠️ Tunnel {local_port} exited rc={rc}; retrying in 5s", flush=True)
        except asyncio.CancelledError:
            if proc is not None:
                await stop_process(proc)
            raise
        except Exception as e:
            print(f"⚠️ Tunnel {local_port} error: {e}", flush=True)

        await asyncio.sleep(5)


async def reconcile_loop():
    global tasks

    while not stopping:
        desired_rows = parse_instances()
        desired = {
            row["local_port"]: (row["host"], row["ssh_port"], row["instance_id"])
            for row in desired_rows
        }

        # Stop removed or changed mappings.
        for local_port, state in list(tasks.items()):
            desired_key = desired.get(local_port)
            if desired_key is None or desired_key != state["key"]:
                reason = "removed" if desired_key is None else "changed"
                print(f"🧹 Stopping tunnel {local_port} ({reason})", flush=True)
                state["task"].cancel()
                try:
                    await state["task"]
                except asyncio.CancelledError:
                    pass
                tasks.pop(local_port, None)

        # Start new mappings.
        by_port = {row["local_port"]: row for row in desired_rows}
        for local_port, key in desired.items():
            if local_port in tasks:
                continue
            task = asyncio.create_task(tunnel_worker(by_port[local_port]))
            tasks[local_port] = {"key": key, "task": task}

        await asyncio.sleep(SCAN_SECONDS)


async def main():
    global stopping
    loop = asyncio.get_running_loop()

    def request_stop():
        global stopping
        stopping = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, request_stop)
        except NotImplementedError:
            pass

    print(f"🚀 Tunnel Manager watching {INSTANCES_FILE} every {SCAN_SECONDS}s", flush=True)

    await reconcile_loop()

    for state in list(tasks.values()):
        state["task"].cancel()
    await asyncio.gather(
        *(state["task"] for state in tasks.values()),
        return_exceptions=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
