# vast-stack — production stack cho Vast.ai + CLIProxyAPI

Stack refactor theo hướng production: **application code tách khỏi compose**,
cấu hình tập trung ở `.env`, `config-sync` là service duy nhất được ghi CPA
config (atomic + validate + rollback). KHÔNG còn regex-edit YAML gây corrupt.

## Cấu trúc

```
vast-stack/
├── compose.yml          # orchestration thuần (không nhúng code)
├── .env.example         # copy → .env (hoặc điền Portainer env)
├── tunnel/              # Dockerfile + tunnel_manager.py
├── gateway/             # Dockerfile + app.py (lifecycle + routing)
├── config-sync/         # Dockerfile + sync.py (CPA config + VLLM keys)
└── README.md
```

## Deploy qua Portainer (đúng cách, không `docker compose up` thủ công)

### Bước 0 — TẮT 3 container cũ (tạo tạm lúc fix)

> ⚠️ Chạy lệnh này SẼ NGẮT CPA → mọi client qua CPA (Hermes, Codex…) mất kết nối
> tạm thời. Làm khi sẵn sàng; sau khi stack mới lên, CPA quay lại nguyên trạng.

```bash
# Trên CT101 (qua Proxmox shell: pct enter 101)
docker stop cli-proxy-api vast-gateway vast-tunnel
docker rm cli-proxy-api vast-gateway vast-tunnel
```

Sau đó xoá stack tạm trong Portainer nếu có (Stacks → tên stack → Remove), để
tránh conflict tên.

### Bước 1 — Push source lên git (bắt buộc, vì compose dùng build: ./…)

```bash
cd vast-stack
git init -b main
git add -A
git commit -m "vast-stack production"
# tạo repo trên GitHub rồi:
git remote add origin git@github.com:<user>/vast-stack.git
git push -u origin main
```

### Bước 2 — Portainer: Stacks → Add stack → "Build from git"

- Repository URL: `https://github.com/<user>/vast-stack.git`
- Compose path: `compose.yml`
- Environment variables: điền theo `.env.example` (VAST_API_KEY,
  API_LLM_SERVER, MANAGEMENT_PASSWORD, …)
- Deploy the stack.

Portainer tự clone repo, **build 3 image** (tunnel/gateway/config-sync) từ
Dockerfile, pull `eceasy/cli-proxy-api:latest` (if_not_present), tạo network.

### Bước 3 — Kiểm tra

```bash
docker ps --format '{{.Names}}  {{.Status}}'
docker logs --tail 20 config-sync      # xem "CPA config updated" hoặc DRY RUN
docker logs --tail 10 vast-tunnel      # xem "🔗 Tunnel …"
curl -s http://10.21.1.101:8317/v1/models -H "Authorization: Bearer $API_LLM_SERVER" | head -c 200
```

## Kiến trúc & an toàn

```
.env → config-sync (validate → backup → atomic replace) → CPA config
                                   ↘ SSH → VLLM nodes (API key)
vast-gateway → chỉ lifecycle/routing (KHÔNG quyền ghi CPA config, mount :ro)
vast-tunnel  → chỉ SSH tunnel
cli-proxy-api → chỉ LLM proxy (+ plugin vast-cluster-bench tự dò tunnel)
```

- **Atomic write**: ghi temp → fsync → rename; nếu YAML invalid → không đụng
  config đang chạy.
- **Rollback**: mỗi lần update tự backup `config.yaml.bak`.
- **Change detection**: `sync-state.json` lưu hash API key; không đổi thì không
  làm gì.
- **DRY_RUN=true**: config-sync chỉ log "would update", không ghi.
- Gateway mount config `:ro` — không bao giờ corrupt YAML nữa.

## Lệnh vận hành

```bash
# update stack sau khi sửa code trong repo (Portainer tự pull khi redeploy)
# hoặc thủ công:
cd vast-stack && docker compose --env-file .env up -d --build

# logs
docker logs -f vast-gateway
docker logs -f config-sync
docker logs -f vast-tunnel

# restart
docker restart cli-proxy-api vast-gateway vast-tunnel config-sync

# down/up toàn bộ
docker compose --env-file .env down
docker compose --env-file .env up -d --build
```
