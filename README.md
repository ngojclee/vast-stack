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

## LiteLLM (audio gateway — chạy song song CPA)

`litellm` + `litellm-db` là 2 service bổ sung: LiteLLM lo **TTS/STT đa
provider + virtual key + GUI chi phí**, CPA giữ nguyên text/OAuth/vision
(plugin agy-identity-bridge…). LiteLLM delegate text về CPA qua
`model_name: cpa/*` → dùng chung 1 cổng :4000 nếu muốn.

**Setup 1 lần trên server (Portainer env đã có biến, config file copy tay):**
```shell
# trên CT101 (pct enter 101) — <stack-dir> là nơi Portainer clone repo
mkdir -p /home/Docker/litellm
cp <stack-dir>/litellm/config.yaml /home/Docker/litellm/config.yaml
```
Sửa `litellm/config.yaml` trong repo → redeploy → cp lại lệnh trên.
(Design giống CPA: file config trên host là source-of-truth, compose chỉ mount.)

**Env cần thêm:** mục LITELLM trong `.env.example` — bắt buộc
`LITELLM_MASTER_KEY` + `LITELLM_DB_PASSWORD`; còn lại điền key provider
audio định dùng (OPENAI / ELEVENLABS / GROQ / GEMINI / DEEPGRAM).

**Dùng:**
- API: `http://<host>:4000/v1` — key = `LITELLM_MASTER_KEY` hoặc virtual key tạo trong GUI
- GUI: `http://<host>:4000/ui` (login = master key)
- TTS: POST `/v1/audio/speech` — model `gpt-4o-mini-tts` / `eleven_turbo_v2_5` / `gemini-tts`
- STT: POST `/v1/audio/transcriptions` — model `whisper-1` / `whisper-large-v3-turbo` / `deepgram-nova3`
- Text qua CPA: model `cpa/philbert440/Qwen3.8-27B-Uncensored-Aggressive-W4A16-AWQ` (khớp mọi model CPA, không cần khai báo từng cái)
