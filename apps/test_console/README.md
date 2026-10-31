# Mobile Test Console

Web UI tiếng Việt cho tester điều khiển ARTEMIS trên điện thoại Android thật.
Nằm ngay trong repo ARTEMIS (`apps/test_console/`), dùng song song với UI gốc
(`artemis ui`, cổng 8000).

Không cần source app Android: console chỉ gọi `artemis run ... --locked-app <package>`,
nên dùng được cho bất kỳ app nào bạn nhập ở ô **Package**.

## ⚡ Chạy nhanh (3 lệnh)

```bash
# 1) Clone
git clone <repo-url> artermis-pro && cd artermis-pro

# 2) Cài dependency + tạo .env (điền API key; thêm ARTEMIS_KB_DSN nếu dùng tab Knowledge)
uv sync --extra knowledge
cp .env.example .env && open -e .env

# 3) Chạy web + tunnel Knowledge — tự mở http://127.0.0.1:8787
sh apps/test_console/run.sh
```

- `run.sh` **tự gọi tunnel KB (`ensure`) rồi mới khởi động web** ⇒ bình thường chỉ cần lệnh số 3.
- Tunnel riêng (kiểm tra / mở lại khi đứt): `sh scripts/kb_tunnel.sh status | start | restart | ensure`
- **Tunnel KB cần gì** (không cài thêm gói nào): **một file khoá SSH private** tới VPS — mặc định
  `~/.ssh/greencloud`, đổi bằng `KB_VPS_KEY=<đường dẫn>` khai trong `.env` hoặc export trong shell
  (`KB_VPS_HOST`, `KB_VPS_USER` để đổi VPS/user; mặc định `root@103.199.18.222`, tunnel
  `127.0.0.1:5433 → VPS:5432`).
  - Máy **chưa có khoá**: chép khoá private qua kênh an toàn + `chmod 600`, hoặc tạo khoá mới
    (`ssh-keygen -t ed25519 -f ~/.ssh/greencloud`) rồi nhờ người giữ VPS thêm khoá public vào
    `root@VPS:~/.ssh/authorized_keys`.
  - Không có SSH vào VPS ⇒ chạy không KB: `KB_TUNNEL=0 sh apps/test_console/run.sh` (web vẫn lên,
    chỉ tab Knowledge báo không dùng được).
- **Có 2 cách dùng KB — không nhất thiết phải SSH**: (a) *đọc thẳng database* bằng
  `ARTEMIS_KB_DSN` (cần đường mạng tới Postgres ⇒ đó là lý do có tunnel SSH); hoặc (b) *gọi console
  ở xa qua HTTP*: `ARTEMIS_KB_API=https://<host>` + `ARTEMIS_KB_API_KEY=<key>` ⇒ máy tester **không
  cần SSH/DB/khoá embedding**. Endpoint `POST {ARTEMIS_KB_API}/api/kb/retrieve` (UA `ARTEMIS/1.0`,
  `Authorization: Bearer <key>`). Máy phục vụ vẫn cần DB; router `/api/kb/*` **không có auth riêng**
  (sửa/xoá được workflow) ⇒ chặn ở proxy, không mở trần ra Internet.
- **Quản trị KB cần đăng nhập**: tab Knowledge có form đăng nhập, tài khoản mặc định
  `admin@admin` / `admin` (đổi bằng `KB_ADMIN_EMAIL` / `KB_ADMIN_PASSWORD`; ký token bằng
  `KB_AUTH_SECRET`; hạn token `KB_AUTH_TTL_SECONDS`, mặc định 12 giờ). Chỉ tài khoản này được
  **thêm/sửa/xoá** workflow; **đọc** (`/retrieve`, danh sách) vẫn không cần tài khoản để agent ở máy
  tester gọi được. `KB_AUTH_DISABLED=1` chỉ dành cho máy dev chạy một mình.
- **DSN**: `.env` cần `ARTEMIS_KB_DSN=postgresql://artemis:<mật-khẩu>@127.0.0.1:5433/artemis_kb`
  (`.env.example` không có sẵn). Khoá SSH không thay thế dòng này. Embedding dùng
  `GEMINI_API_KEY`/`GOOGLE_API_KEY`.
- **Thiếu khoá SSH hoặc DSN: web vẫn chạy**, chỉ tab Knowledge báo không dùng được ⇒ mở lại bằng
  `sh scripts/kb_tunnel.sh start` sau khi đã có khoá.
- Cần có: `adb` trong PATH + điện thoại Android đã bật **USB debugging** (đã bấm *Allow*).
- Chưa có `uv`: `brew install uv` (macOS) hoặc `curl -LsSf https://astral.sh/uv/install.sh | sh`.
- Tắt bước tunnel khi chạy: `KB_TUNNEL=0 sh apps/test_console/run.sh`.

## Tính năng

- **Timeline bước chạy**: ảnh Trước → Sau, nhãn hành động tiếng Việt, bấm ảnh phóng to.
- **Kết luận rõ ràng**: ĐẠT / KHÔNG ĐẠT / ĐÃ DỪNG, lỗi chính, số bước, thời gian, chi phí.
- **Thông báo khi xong**: toast + âm thanh + Web Notification.
- Ô **Prompt / Mục tiêu** tự do: nhập prompt bất kỳ, sửa prompt mẫu, nút
  **Prompt tự do**, lưu nháp, `⌘/Ctrl+Enter` để chạy.
- Chọn thiết bị đang cắm USB (adb); package mặc định `com.example.app` (đổi ở ô **Package**).
- 7 kịch bản mẫu dựng sẵn: smoke khởi động, màn hình đăng nhập, danh sách
  thiết bị, bật/tắt công tắc, voicebox/học lệnh, thêm thiết bị QR/BLE, thông báo.
- Chạy **Flash** (nhanh) hoặc **Pro** (có plan/verify).
- Xem **màn hình điện thoại trực tiếp** (chụp 2 giây/lần) và **log realtime**.
- Dừng task **thật sự**: huỷ cả trên Artemis Daemon (nơi thực thi) lẫn tiến
  trình CLI, xem **lịch sử** và log từng lần chạy.
- Kiểm tra LLM (`/models` + 1 chat 1 token) — phát hiện key hết hạn/hết credit.
- **Chặn ngân sách**: giới hạn request, `max_tokens`, chi phí task/ngày, thời gian.
- **Khoá truy cập API**: `/llm/v1` chỉ mở khi client gửi đúng `CONSOLE_ACCESS_KEY`
  (mặc định `21092003`) — key LLM thật không rời máy chạy console.
- **Knowledge — Workflow RAG**: tester nhập prompt, console gọi Google Embedding, tìm workflow
  gần nhất trong PostgreSQL rồi trả về **prompt hoàn chỉnh** (prompt gốc + hướng dẫn từng bước).
  Chỉ máy chạy console cần database và khoá embedding; máy tester chỉ cần ARTEMIS và điện thoại.
- **Chuẩn bị prompt bằng AI** (`⚡ Chuẩn bị prompt`): RAG cung cấp kiến thức, LLM tổng hợp thành
  **một prompt thực thi ngắn** (không bước, không toạ độ), tester duyệt/sửa rồi mới chạy. Xem
  *Chuẩn bị prompt bằng AI* bên dưới.


## Chạy

Console nằm trong repo ARTEMIS và mặc định dùng chính `.venv`, `.env`,
`config/artemis.jsonc` của repo đó.

```bash
# Chạy được từ bất kỳ thư mục nào
cd ~/Developer/artermis-pro
sh apps/test_console/run.sh          # tự mở http://127.0.0.1:8787
```

- Clone mới chưa có `.venv`? Chạy `uv sync` một lần, hoặc để `run.sh` tự gọi `uv run`.
- Muốn drive một checkout ARTEMIS khác: `ARTEMIS_DIR=/path/to/artemis sh apps/test_console/run.sh`.
- Đổi app đích: `TEST_APP_PACKAGE=com.example.app sh apps/test_console/run.sh`.
- Đổi cổng: `ARTEMIS_WEB_PORT=8788 sh apps/test_console/run.sh`.

Console chỉ gọi `artemis run` nên **không cần source app**.

## Chạy kèm Knowledge base (KB)

KB là **PostgreSQL + pgvector** chứa các Workflow của app. Console dùng nó cho tab **Knowledge**
(chuẩn bị prompt bằng RAG) và để ghép kiến thức vào prompt trước mỗi lần chạy. KB **không bắt buộc**:
không có KB thì task vẫn chạy, chỉ là prompt không có kiến thức — y hệt một lần chạy không có gì để chèn.

### Chọn đúng một trong ba cách

| Cách | Khi nào dùng | Cần gì |
| --- | --- | --- |
| **KB local qua SSH tunnel** | Máy dev: muốn tab Knowledge + tra KB ngay trên máy | khoá SSH tới VPS, `uv sync --extra knowledge` |
| **KB trên VPS qua HTTPS** | Máy tester / máy chỉ cắm điện thoại | 2 biến `ARTEMIS_KB_API` + `ARTEMIS_KB_API_KEY` (xem mục **B**) |
| **Không dùng KB** | Chạy nhanh, hoặc máy chạy vai `server` (VPS nối PostgreSQL trực tiếp) | `KB_TUNNEL=0 sh apps/test_console/run.sh` |

### Cách 1 — KB local qua tunnel (3 bước)

```bash
# 1) Cài phần KB của runtime: psycopg + pgvector (thiếu 2 gói này ⇒ KB báo "chưa sẵn sàng")
uv sync --extra knowledge

# 2) Khai báo KB trong .env (thêm vào .env đang dùng, KHÔNG commit)
#    ARTEMIS_KB_DSN=postgresql://artemis:<mat-khau>@127.0.0.1:5433/artemis_kb   ← bắt buộc để có KB
#    ARTEMIS_KB_EMBEDDING_MODEL=gemini-embedding-2   # tuỳ chọn (mặc định đã là gemini-embedding-2)
#    ARTEMIS_KB_TOP_K=3                              # tuỳ chọn (mặc định 3 workflow vào prompt)

# 3) Mở tunnel rồi chạy console — run.sh tự gọi `ensure` nên thường không phải làm gì
sh apps/test_console/run.sh
#   Knowledge base: tunnel sẵn sàng (127.0.0.1:5433).   ← dấu hiệu KB dùng được
```

Tunnel đi qua SSH nên **cần khoá** `~/.ssh/greencloud` (hoặc `KB_VPS_KEY`); đổi host/user/cổng bằng
`KB_VPS_HOST`, `KB_VPS_USER`, `KB_LOCAL_PORT`. Nếu máy không có khoá, `run.sh` chỉ **cảnh báo** rồi
vẫn chạy console (tab Knowledge sẽ báo KB không dùng được) — hoặc đặt `KB_TUNNEL=0` để bỏ hẳn bước này.

**Kiểm tra:**

```bash
sh scripts/kb_tunnel.sh status         # Tunnel đang chạy (pid …): 127.0.0.1:5433 -> <vps>:5432
curl -s http://127.0.0.1:8787/api/kb/status | python3 -m json.tool
#   "available": true, "database": "artemis@127.0.0.1:5433/artemis_kb", "workflow_count": 89, …
```

| Triệu chứng | Cách xử lý |
| --- | --- |
| `CẢNH BÁO: chưa mở được tunnel tới Knowledge base` | Xem `sh scripts/kb_tunnel.sh status`; thiếu khoá SSH hoặc VPS không tới được |
| Tab Knowledge: `degraded_reason` chứa `Không kết nối được PostgreSQL` | Tunnel chưa mở → `sh scripts/kb_tunnel.sh ensure` |
| `available: false`, `degraded_reason: "Chưa cấu hình ARTEMIS_KB_DSN"` | Chưa thêm `ARTEMIS_KB_DSN` vào `.env` |
| `available: false` vì thiếu module | `degraded_reason` ghi `Knowledge base không dùng được: No module named 'psycopg'` → chạy `uv sync --extra knowledge` rồi khởi động lại console |
| `status` báo "đang chạy" mà KB vẫn lỗi | Kết nối SSH nửa chết (pid còn, cổng đóng) → `sh scripts/kb_tunnel.sh restart` |
| RAG lỗi khi chuẩn bị prompt | Thiếu/sai `GEMINI_API_KEY` (embedding chỉ chạy trên máy chạy console) |

Chi tiết tunnel (đổi cổng, chạy tay, các lỗi SSH thường gặp) nằm ở README gốc của repo, mục
*Knowledge base & SSH tunnel*.

## Cài trên máy khác

Chọn đúng một trong ba tình huống:

### A. Máy đó chạy console luôn (cắm điện thoại vào chính máy đó)

```bash
# 1) Công cụ cần có
xcode-select --install        # macOS: git + compiler (Linux: apt install git build-essential)
brew install uv               # hoặc: curl -LsSf https://astral.sh/uv/install.sh | sh
# adb: Android Studio → SDK Manager → Android SDK Platform-Tools
#      hoặc: brew install android-platform-tools

# 2) Lấy code
git clone https://github.com/sep21th03/artermis-pro.git ~/Developer/artermis-pro
cd ~/Developer/artermis-pro

# 3) Cài dependency Python (tạo .venv) — chạy một lần
uv sync

# 4) Tạo .env — chỉ 4 dòng
cat > .env <<'EOF'
OPENAI_API_KEY=<key thật của bạn>
OPENAI_BASE_URL=http://127.0.0.1:8787/llm/v1
ARTEMIS_UPSTREAM_BASE_URL=https://api.vilao.ai/v1
CONSOLE_ACCESS_KEY=21092003
EOF
# Muốn dùng KB (tab Knowledge + ghép kiến thức): thêm `uv sync --extra knowledge` và
# `ARTEMIS_KB_DSN` — xem mục "Chạy kèm Knowledge base (KB)" ở trên.

# 5) Cắm điện thoại, bật USB debugging, bấm Allow trên máy
export PATH="$HOME/Library/Android/sdk/platform-tools:$PATH"
adb devices -l                 # phải thấy "device" (không phải "unauthorized")

# 6) Chạy
sh apps/test_console/run.sh    # tự mở http://127.0.0.1:8787
```

### B. Máy đó chỉ cắm điện thoại, console chạy ở máy khác (khuyến nghị cho nhóm)

Máy tester **không cần key thật, không cần PostgreSQL, không cần khoá embedding**. Làm
bước 1–3 như trên, rồi:

```bash
cat > .env <<'EOF'
OPENAI_API_KEY=21092003                                  # khoá truy cập, xin từ quản trị
OPENAI_BASE_URL=http://<ip-máy-chạy-console>:8787/llm/v1
ARTEMIS_KB_API=http://<ip-máy-chạy-console>:8787         # nhận prompt hoàn chỉnh
ARTEMIS_KB_API_KEY=21092003                              # cùng khoá truy cập ở trên
EOF
```

Máy chạy console phải mở cổng cho LAN:

```bash
ARTEMIS_WEB_HOST=0.0.0.0 sh apps/test_console/run.sh
```

rồi mở firewall cho cổng 8787 — **chỉ trong LAN, đừng mở ra Internet**.

Hoặc để script làm hết (kiểm tra công cụ → `uv sync` → dựng `.env` → **thử gọi console** để biết ngay
URL/khoá có đúng):

```bash
sh scripts/install_tester.sh --console http://<ip-máy-chạy-console>:8787 --key 21092003
sh scripts/install_tester.sh --console https://sep21th03.com/artemis --key 21092003   # console trên VPS
```

Mặc định script ghi **cả file `.env`** chứ không phải 4 dòng: lấy mẫu theo thứ tự `--from <file>` →
`.env` của repo (nếu có) → `.env.example`, rồi

- **ghi đè 5 biến** thuộc máy này: `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `CONSOLE_ACCESS_KEY`,
  `ARTEMIS_KB_API`, `ARTEMIS_KB_API_KEY`;
- **bỏ giá trị** các khoá chỉ thuộc máy chạy console (`GEMINI_API_KEY`, `ARTEMIS_KB_DSN`,
  `KB_ADMIN_PASSWORD`, `KB_AUTH_SECRET`, `KB_VPS_KEY`) ⇒ file sinh ra **gửi cho người khác được**;
- giữ nguyên phần còn lại ⇒ máy nhận có **cùng model, cùng chuỗi xoay quota, cùng hạn mức** như máy
  đang cấu hình, nên không phải chép `.env` đi gửi từng người.

Ghi bằng `chmod 600`. `--minimal` để chỉ ghi 4 dòng tối thiểu; `--keep-secrets` giữ cả khoá bí mật (chỉ
khi máy này cũng chạy console — **đừng gửi file đó cho ai**); `--dry-run` in ra mà không ghi; `--force`
ghi đè `.env` đang có (bản cũ được sao lưu `.bak.<thời-gian>`); `--no-sync` / `--no-verify` bỏ hai bước
tương ứng; `--help` xem đủ tham số.

**Vì sao chỉ một máy cần PostgreSQL.** Toàn bộ kiến thức — các Workflow của app — nằm trong
một PostgreSQL trên máy chạy console (hoặc trên VPS của bạn). Máy tester trước mỗi lần chạy hỏi
đúng một câu:


```
POST http://<ip-console>:8787/api/knowledge/context
     {"goal": "...", "app_package": "com.iot.hunonic", "run_id": "..."}
  →  {"prompt": "<khối kiến thức y hệt console sẽ tự chèn>", "found": true, ...}
```

Nhờ vậy hai máy chạy ra cùng một prompt, mà chỉ một máy cần database và khoá. Embedding
(việc gọi Gemini) cũng chỉ xảy ra trên máy console — máy tester không cần
`GEMINI_API_KEY`.

| Máy | Cần cài | Cần có |
| --- | --- | --- |
| Chạy console | ARTEMIS + ADB + PostgreSQL | `ARTEMIS_KB_DSN`, `GEMINI_API_KEY`, key LLM thật |
| Tester | ARTEMIS + ADB | 3 biến ở trên |

Nếu console tắt hoặc không gọi được, ARTEMIS **chạy tiếp không có kiến thức** (giống hệt
khi chưa bật KB) — không bao giờ làm hỏng một lần chạy test.

### C. Đặt cả máy chủ lên VPS (cách đang dùng)

Knowledge base, **AI proxy** và **API RAG** đều nằm trên VPS. Máy nào cắm điện thoại cũng chỉ
là *client*: không database, không khoá LLM, không khoá embedding.

```
VPS  sep21th03.com  (103.199.18.222)
  nginx (443, TLS)  ──/artemis/──┐
  PostgreSQL 14 + pgvector       │   vai trò server
  database artemis_kb            └── console 127.0.0.1:8787
                                        POST /llm/v1/...            (AI proxy, có khoá)
                                        POST /api/knowledge/context (API RAG, có khoá)

Máy dev (Mac)  ── SSH tunnel ──→ VPS:5432      ← chỉ để quản trị KB (tab Knowledge)
               ── HTTPS ──────→ sep21th03.com/artemis   ← LLM + RAG

Máy tester (ARTEMIS + ADB, không gì khác)
               ── HTTPS ──────→ sep21th03.com/artemis   ← LLM + RAG
```

#### Vì sao console phải tách vai trò

Console làm hai việc rất khác nhau, và trộn chúng lên Internet là lỗ hổng:

| | `server` (VPS) | `edge` (máy cắm điện thoại) |
| --- | --- | --- |
| AI proxy `/llm/v1` | ✅ | — |
| API RAG `/api/knowledge/context` | ✅ | — |
| API chuẩn bị prompt `/api/knowledge/prepare` | ✅ | — |
| Quản trị KB `/api/kb/*` | — | ✅ |
| Điều khiển thiết bị, `/api/run`, `/api/stop` | **không đăng ký** | ✅ |
| Giao diện | trang trạng thái | giao diện đầy đủ |

`POST /api/run` chạy `artemis run` và `POST /api/stop` giết tiến trình. Trên VPS chúng phải
**không tồn tại** trong bảng route, chứ không chỉ bị từ chối — middleware sót một đường là mất
hết. Đặt `CONSOLE_ROLE=server` là đủ; `all` (mặc định) giữ nguyên hành vi cũ cho máy dev chạy
một mình.

`/api/kb/*` cũng bị loại khỏi VPS vì nó sửa và xoá được workflow mà **không có khoá truy cập
riêng**. Người vận hành quản trị KB từ máy dev, qua SSH tunnel tới cùng database đó.

#### Cài trên VPS

```bash
# 1) pgvector cho PostgreSQL 14 (Ubuntu 22.04) — repo chính thức PGDG
apt-get install -y curl ca-certificates
install -d /usr/share/postgresql-common/pgdg
curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
  https://www.postgresql.org/media/keys/ACCC4CF8.asc
echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
https://apt.postgresql.org/pub/repos/apt jammy-pgdg main" > /etc/apt/sources.list.d/pgdg.list
apt-get update && apt-get install -y postgresql-14-pgvector

# 2) role + database (chỉ THÊM, không đụng database khác trên VPS)
su - postgres -c "psql -c \"CREATE ROLE artemis LOGIN PASSWORD '<mat-khau-manh>';\""
su - postgres -c "createdb -O artemis -E UTF8 artemis_kb"

# 3) runtime: uv + Python 3.12 (repo yêu cầu >=3.12, Ubuntu 22.04 chỉ có 3.10)
#    Đặt Python NGOÀI /root — /root là mode 700 nên user `artemis` không đi qua được,
#    và systemd sẽ chết với status=203/EXEC.
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH" UV_PYTHON_INSTALL_DIR=/opt/uv-python
uv python install 3.12

# 4) code + dependency
rsync -az --exclude='.venv/' --exclude='.git/' --exclude='node_modules/' \
      ./ root@<vps>:/opt/artemis/
cd /opt/artemis && uv sync --extra knowledge --python 3.12
useradd --system --shell /usr/sbin/nologin --home-dir /opt/artemis artemis
chown -R artemis:artemis /opt/artemis

# 5) /opt/artemis/.env  (600, chủ sở hữu artemis) — xem mẫu bên dưới

# 6) systemd
systemctl enable --now artemis-console
```

`/opt/artemis/.env` trên VPS:

```bash
CONSOLE_ROLE=server
ARTEMIS_WEB_HOST=127.0.0.1        # nginx proxy vào; cổng 8787 không ra Internet
CONSOLE_ACCESS_KEY=<khoá truy cập>
OPENAI_API_KEY=<key LLM THẬT>     # proxy giữ nó, máy tester không bao giờ thấy
ARTEMIS_UPSTREAM_BASE_URL=https://api.vilao.ai/v1
GEMINI_API_KEY=<key embedding>
ARTEMIS_KB_DSN=postgresql://artemis:<mat-khau>@127.0.0.1:5432/artemis_kb
```

⚠️ **Đừng chép `OPENAI_BASE_URL` sang VPS.** Trên máy dev nó trỏ về chính console đó; chép sang
VPS là proxy gọi vòng lại chính nó.

#### nginx — HTTPS thay vì HTTP trần

Khoá truy cập đi kèm mỗi request, nên nó phải đi qua TLS. Thêm vào server block HTTPS sẵn có:

```nginx
location ^~ /artemis/ {
    proxy_pass http://127.0.0.1:8787/;   # dấu / cuối: cắt bỏ tiền tố /artemis/
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;                 # proxy LLM trả SSE: đệm là client thấy "đứng hình"
    proxy_cache off;
    proxy_read_timeout 900s;             # một lượt chat dài có thể mất vài phút
    proxy_send_timeout 900s;
    client_max_body_size 10m;
}
```

`^~` để không location regex nào chạy trước; đặt trước `location /` để không bị nuốt.

#### Máy dev và máy tester

```bash
# .env trên máy dev VÀ trên mỗi máy tester
OPENAI_BASE_URL=https://sep21th03.com/artemis/llm/v1
ARTEMIS_KB_API=https://sep21th03.com/artemis
ARTEMIS_KB_API_KEY=<khoá truy cập>
OPENAI_API_KEY=<khoá truy cập>     # KHÔNG phải key thật
```

Máy dev thêm một dòng để quản trị KB, vì tab Knowledge cần đọc database trực tiếp:

```bash
sh scripts/kb_tunnel.sh start
# ARTEMIS_KB_DSN=postgresql://artemis:<mat-khau>@127.0.0.1:5433/artemis_kb
```

**Vì sao tunnel chứ không mở cổng 5432:** database này không có TLS, và một cổng PostgreSQL mở
ra Internet là mời người lạ xoá kiến thức. Tunnel giữ 5432 đóng với thế giới (`pg_hba` chỉ cho
`127.0.0.1/32` dùng `scram-sha-256`), mà máy dev vẫn thấy nó như một PostgreSQL tại chỗ.

#### Hai cái bẫy đã gặp thật

- **Cloudflare chặn User-Agent của Python.** `sep21th03.com` đi qua Cloudflare; cùng một request,
  UA mặc định `Python-urllib/3.x` trả **403 (error 1010)** còn `ARTEMIS/1.0` trả 200. ARTEMIS nay
  tự gửi `User-Agent: ARTEMIS/1.0`. Triệu chứng khi thiếu header rất khó đoán: log chỉ nói
  *"không chèn kiến thức"*, y hệt một lần chạy không có gì để chèn.
- **Cài pgvector kéo theo nâng cấp gói lõi.** Repo PGDG có bản `postgresql-14` mới hơn bản của
  Ubuntu, nên `apt-get install postgresql-14-pgvector` sẽ nâng cấp luôn PostgreSQL (cùng 14.24,
  khác nhà build) và **restart PostgreSQL**. Trên VPS có app khác dùng chung thì nên làm lúc
  vắng người dùng.

### Kiểm tra sau khi cài

```bash
curl -s http://127.0.0.1:8787/api/health | python3 -m json.tool | head -25
#   "devices": [ { "serial": "...", "state": "device" } ]   ← thấy điện thoại là được
curl -s http://127.0.0.1:8787/api/packages | head -c 200      # danh sách app đã cài
```

| Triệu chứng | Cách xử lý |
| --- | --- |
| `Không tìm thấy Artemis tại ...` | chưa chạy `uv sync` (thiếu `.venv`) |
| `Không có thiết bị` | cáp lỏng / chưa bật USB debugging / chưa bấm **Allow** |
| `401 invalid_api_key` khi chạy task | `OPENAI_API_KEY` phải là **khoá truy cập** (`21092003`), không phải key thật |
| Không mở được `http://<ip>:8787` từ máy khác | console chưa đặt `ARTEMIS_WEB_HOST=0.0.0.0`, hoặc firewall chặn |
| App không mở | xem *Cài app lên điện thoại* + *Task chạy nhưng app không mở* |
| `Failed to start scrcpy recording` | vô hại; muốn ghi video thì `brew install scrcpy` |
| `available: false` ở `/api/kb/status` | KB không bật — xem *Chạy kèm Knowledge base (KB)* |

### Tuỳ chọn

| Muốn | Cài thêm |
| --- | --- |
| Ghi video bước chạy | `brew install scrcpy` |
| Test app React Native bản debug | Node 20 + Metro (`yarn start` + `adb reverse tcp:8081 tcp:8081`) |
| Dùng model local (0đ) | Ollama + `LOCAL_LLM=1 sh apps/test_console/run.sh` |

## Cài app lên điện thoại

Console **không cài app giúp bạn** — nó chỉ điều khiển app đã có sẵn trên máy.
Cài app trước, rồi nhập tên package vào ô **Package** trên UI.

### Cách 1 — Cài từ file APK (nhanh nhất)

```bash
export PATH="$HOME/Library/Android/sdk/platform-tools:$PATH"
adb devices -l                                   # phải thấy "device" (không phải "unauthorized")
adb install -r /duong/dan/app-debug.apk          # -r: ghi đè bản đang có

# App dạng nhiều APK (split từ AAB):
adb install-multiple -r /duong/dan/*.apk
```

Nếu máy báo `unauthorized`: mở khoá điện thoại và bấm **Allow** ở hộp thoại
*Allow USB debugging*.

### Cách 2 — Build & cài từ source

```bash
# Android native
cd /path/to/your-app && ./gradlew installDebug

# React Native
cd /path/to/your-app && npx react-native run-android
```

### Cách 3 — App React Native bản debug thì PHẢI có Metro

Bản **debug** không nhúng JS bundle, nên phải chạy Metro và `adb reverse`
trước khi test, nếu không app chỉ hiện màn hình đỏ *"Unable to load script"*:

```bash
cd /path/to/your-app
export PATH="$HOME/.nvm/versions/node/v20.19.5/bin:/opt/homebrew/bin:$PATH"
yarn start                        # giữ terminal này chạy suốt lúc test
adb reverse tcp:8081 tcp:8081     # bắt buộc với bản debug
```

### Kiểm tra đã cài đúng

```bash
adb shell pm list packages | grep -i hunonic                                # tìm package
adb shell monkey -p com.iot.hunonic -c android.intent.category.LAUNCHER 1   # mở app
adb uninstall com.iot.hunonic                                               # gỡ app
```

Nhập đúng package vừa tìm được vào ô **Package** của console rồi bấm **Chạy test**.
Console **từ chối chạy** nếu app chưa cài trên máy (kèm gợi ý tên gần đúng), và ô
Package gợi ý sẵn danh sách app đã cài — chạy nhầm package thì ARTEMIS chỉ thử mở
3 lần × 15s rồi bỏ cuộc, tester thấy như "app không chạy".
Khi console báo *"App chưa tải được JS bundle — kiểm tra Metro và adb reverse"*
thì quay lại Cách 3.

Yêu cầu: ARTEMIS đã cài dependency (`.venv`), có `adb`, điện thoại đã bật
**USB debugging**. Hướng dẫn tổng thể: [README.md](../../README.md) mục 16.

## LLM: không cần cài key

Máy tester **không cần** key LLM. Console giữ key thật và tự thay vào mỗi request,
nên `.env` của máy tester chỉ có 2 dòng:

```env
OPENAI_API_KEY=21092003                                  # khoá truy cập, xin từ quản trị
OPENAI_BASE_URL=http://<ip-máy-chạy-console>:8787/llm/v1 # chạy chung 1 máy thì để 127.0.0.1
```

Máy nào không khai `OPENAI_BASE_URL` sẽ gọi thẳng nhà cung cấp và **không** được
chặn ngân sách. Chạy console cùng máy thì không phải cấu hình gì thêm — `.env`
của repo đã sẵn.

Model nằm trong `artemis/config/artemis.jsonc` (hiện tại `ts/gemini-3.5-flash-lite`,
có vision + tool calling). Đổi `.env`/model thì restart Artemis: `artemis restart`.

> Nếu tắt console 8787, ARTEMIS không gọi được LLM vì base URL trỏ vào proxy.

## Khoá truy cập API (chỉ máy chạy console)

Console là proxy giữ key thật, nên `/llm/v1` **bắt buộc** có khoá truy cập — không
có thì ai biết URL cũng tiêu hết ngân sách. **Máy tester không phải cài key gì
thêm**, chỉ dùng khoá truy cập ở mục trên.

ARTEMIS gửi `Authorization: Bearer <OPENAI_API_KEY>`; proxy kiểm tra khoá rồi
**thay** bằng key thật trước khi gọi upstream, nên key thật không bao giờ rời máy
chạy console.

| Tình huống | Kết quả |
| --- | --- |
| Không gửi khoá / khoá sai | `401`, thông báo có hướng dẫn, **không lộ khoá** |
| Gửi khoá truy cập (`21092003`) | Qua proxy, gọi upstream bình thường |
| Gửi đúng key thật (máy chạy console) | Qua proxy — cấu hình cũ không phải đổi |
| `LOCAL_LLM=1` (Ollama) | Miễn khoá, không cần key |

Đổi khoá: sửa `CONSOLE_ACCESS_KEY` trong `.env` rồi restart console.

Kiểm tra nhanh:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8787/llm/v1/models                      # 401
curl -s -H 'Authorization: Bearer 21092003' http://127.0.0.1:8787/llm/v1/models | head -c 120    # 200
curl -s http://127.0.0.1:8787/api/health | python3 -c 'import json,sys; print(json.load(sys.stdin)["access"])'
```

## Luồng chạy

```
Tester nhập prompt gốc
   ↓  POST /api/knowledge/prepare  (khuyến nghị — có LLM tổng hợp, tester duyệt)
Máy chủ: RAG lấy kiến thức → LLM tổng hợp thành Enhanced Prompt ngắn
   ↓  tester xem/sửa trong UI → ✔ Dùng bản này
Tester chạy agent (ADB) với prompt đã duyệt
   ↓  (hoặc, luồng cũ, không qua LLM)
Tester nhập prompt gốc
   ↓  POST /api/knowledge/context   (prompt gốc)
Console: gọi Google Embedding → tìm workflow gần nhất trong PostgreSQL
   ↓  ghép prompt gốc + hướng dẫn của workflow
Prompt hoàn chỉnh trả về cho tester
   ↓
Tester chạy agent (ADB) với prompt hoàn chỉnh đó
```

Cả hai đường đều **không sửa prompt của tester**: prompt gốc luôn nằm nguyên vẹn ở
đầu, phần thêm vào là hướng dẫn/kiến thức ở sau. Không tìm được workflow nào thì trả
lại **đúng prompt gốc**, và tester vẫn chạy được bình thường.

Đây là lý do chỉ **một máy** cần PostgreSQL và khoá embedding: máy đó gọi Google Embedding và
đọc database, còn máy tester chỉ gửi prompt đi rồi nhận prompt về.

## Chuẩn bị prompt bằng AI (nút ⚡ Chuẩn bị prompt)

Đây là bước **bắt buộc trước khi chạy** nếu app có workflow trong KB. Thay vì nối
nguyên khối workflow vào prompt (dài, đầy hướng dẫn UI cứng), máy chủ **hiểu mục
tiêu** rồi tổng hợp kiến thức liên quan thành **một prompt thực thi ngắn**, và tester
duyệt trước khi chạy.

```
Tester nhập prompt gốc
   ↓  POST /api/knowledge/prepare  {prompt, app_package, profile}
Máy chủ: hiểu mục tiêu → truy xuất theo TỪNG việc → phân loại vai trò workflow
   ↓  LLM (qua chính AI proxy này) tổng hợp thành Enhanced Prompt 5 mục
   ↓  10 kiểm tra chất lượng bằng máy
Tester xem/sửa trong ô "prompt agent sẽ nhận"  →  ✔ Dùng bản này  →  Chạy
   ↓  ARTEMIS nhận đúng bản đã duyệt (prompt_override + ARTEMIS_KB_PRESUPPLIED=1)
```

Ví dụ, `mở app và đăng nhập rồi chuyển sang nhà dled` cho ra:

```
KẾ HOẠCH:
1. mở app — chỉ khi app chưa mở — kiến thức: KB chưa có hướng dẫn
2. đăng nhập — chỉ khi chưa đăng nhập — kiến thức: auth.switch_account
3. chuyển nhà dled — kiến thức: KB chưa có hướng dẫn
KIẾN THỨC ỨNG DỤNG: … điều kiện phải kiểm tra trên UI (không được mặc định là đúng) …
BỐI CẢNH / CẢNH BÁO: chỉ là bối cảnh — KHÔNG phải mục tiêu, KHÔNG phải bước phải làm
KB CHƯA CÓ HƯỚNG DẪN: … tự khám phá UI hiện tại, KHÔNG tự suy diễn ra quy trình
TIÊU CHÍ XÁC MINH: - Mục tiêu cuối: nhà hiện tại = dled.
```

Điểm khác biệt so với trước: modal 'Chuyển nhà' chỉ còn là **bối cảnh**, không còn
bị viết thành bước ("nhấn Huỷ"); việc chuyển nhà được báo là **KB chưa có hướng dẫn**
thay vì bịa một quy trình; `dled` được giữ nguyên.

Prompt gốc **luôn** nguyên vẹn ở đầu Enhanced Prompt — LLM chỉ được viết phần kiến
thức phía sau, nên không thể mất mục tiêu hay đổi số điện thoại/tài khoản.

Dòng trạng thái dưới ô prompt cho biết: các việc đã tách, **workflow thực hiện**,
**bối cảnh**, **workflow đã loại**, **việc thiếu kiến thức**, `app`, phiên bản KB,
cache, số lượt LLM, token, và lý do nếu phải dùng bản dự phòng.

| Trạng thái | Nghĩa | Việc cần làm |
| --- | --- | --- |
| `llm` | Đã tổng hợp, qua đủ kiểm tra | đọc rồi Chạy |
| `fallback` | LLM lỗi/quá hạn/không đạt kiểm tra ⇒ bản dựng bằng luật | vẫn chạy được; xem lý do ở dòng trạng thái |
| `no_knowledge` | KB không có workflow nào phục vụ các việc này | thêm workflow/alias trong tab Knowledge |
| `degraded` | Không truy cập được KB | kiểm tra `ARTEMIS_KB_DSN` / `ARTEMIS_KB_API` |

Gọi thẳng từ máy khác (đúng endpoint máy tester dùng):

```bash
curl -s -X POST https://sep21th03.com/artemis/api/knowledge/prepare \
  -H 'Content-Type: application/json' -H 'Authorization: Bearer <khoá truy cập>' \
  -d '{"prompt":"mở app và đăng nhập rồi chuyển sang nhà dled","app_package":"com.iot.hunonic","profile":"pro"}'
# → {"original_prompt":"…","enhanced_prompt":"…","status":"llm","ready":true,
#    "tasks":[{"action":"chuyển nhà","target":"dled","condition":""},…],
#    "matched_workflows":[{"workflow_id":"auth.switch_account","role":"primary",…}],
#    "excluded_workflows":[…],"knowledge_gaps":[…],"warnings":[…],
#    "source":{…},"checks":[…],"metrics":{"llm_calls":2,…}}
```

⚠️ **Endpoint này chỉ có ở `CONSOLE_ROLE=server` và `all`, không có ở `edge`.** Máy
cắm điện thoại không giữ key LLM, nên ở đó nút bấm sẽ báo phải gọi máy chủ (địa chỉ
lấy từ `ARTEMIS_KB_API`, hiện trong `GET /api/health` ở trường `kb_api`). Tinh chỉnh
bằng `ARTEMIS_PREPARE_*` trong `.env` — xem mục cuối `.env.example`.

## Knowledge — Workflow RAG (tab “Knowledge”)

Tab **Knowledge** là nơi duy nhất chứa kiến thức của app. Một *workflow* là một chức năng kèm
các bước, điều kiện trước, và tiêu chí thành công để Checker chấm.

Cách khớp là **lai (hybrid)**: id/alias chính xác + full-text tiếng Việt + vector, hợp nhất bằng
RRF. Chỉ khớp nhờ vector được coi là **máy đoán** và **không** được chèn vào prompt — máy đoán
không được lái agent. Xem *Ba điều cần nhớ* bên dưới.


### Cách dùng

1. Chọn app ở ô đầu tab (app được tạo tự động khi bạn lưu workflow lần đầu; gán
   **package** để agent tìm được đúng app — ví dụ `com.iot.hunonic`).
2. **＋ Workflow mới** → điền id (`auth.logout`), tên, **alias**, các bước, tiêu chí thành
   công → **Lưu**. Embedding được tạo ngay khi lưu.
3. **🔎 Kiểm tra truy xuất**: gõ thử câu tester sẽ nhập (ví dụ `Test đăng xuất`) và xem
   chính xác khối văn bản nào sẽ vào Planner.
4. Chạy test với **🧠 Pro**. Sau khi chạy, tab **📊 Kiến thức đã dùng** hiện workflow nào
   đã được chọn và task kết thúc ra sao.

### Ba điều cần nhớ khi viết workflow

- **Bước là ý định, không phải toạ độ.** Viết “tab ngoài cùng bên phải trên thanh dưới
  cùng”, đừng viết `(648, 1514)`. Operator tự tìm phần tử khớp mô tả trên UI thật; toạ độ
  sẽ sai ngay khi UI đổi.
- **Alias là chỗ đáng đầu tư nhất.** Khớp alias được tính là *bằng chứng*; chỉ khớp vector
  thì chỉ là *máy đoán* và **không được chèn vào Planner**.
- **Tiêu chí thành công là để Checker chấm.** Planner được yêu cầu biến chúng thành check
  line, nên đây là thứ quyết định task báo ĐẠT hay CHƯA RÕ.

### ⚠️ Tab Knowledge chỉ có tác dụng với `--profile pro`

Profile **Flash** chạy `FlashRunner` — một vòng phản ứng đơn, **không có Planner**. Khối
kiến thức không có chỗ nào để tiêu thụ. Console vẫn in cảnh báo rõ khi gặp tình huống này:

```
RAG: chèn kiến thức cho workflow auth.logout của app hunonic (901ms)
⚠ Profile 'flash' chạy FlashRunner (KHÔNG có Planner) nên khối kiến thức này
  không được dùng. Chạy --profile pro để RAG có hiệu lực.
```

### Nhiều workflow thì nó chọn cái nào?

Xếp hạng theo điểm RRF (hợp nhất các kênh), lấy tối đa `ARTEMIS_KB_TOP_K` (mặc định 3), và toàn
bộ khối bị chặn ở `ARTEMIS_KB_CONTEXT_MAX_CHARS` (mặc định 4.000 ký tự).

Workflow chỉ khớp bằng vector được liệt kê riêng ở mục *CÓ THỂ LIÊN QUAN*, dạng **chỉ có tên,
không có bước** — để agent biết chức năng đó tồn tại mà không đi theo một đường có thể lạc đề.

Với app có nhiều workflow (20–30):

| Loại | Nên khai báo |
| --- | --- |
| Chức năng hay dùng | alias rõ (2–4 cách gọi), để nó tự khớp khi prompt nhắc tới |
| Luật của app (modal, cảnh báo) | một workflow `status=verified` mô tả *cái này là gì → phải làm gì* |
| Chức năng ít dùng | để `status=draft`, chỉ nâng lên `verified` khi đã kiểm chứng |

Nếu thấy workflow quan trọng không bao giờ được chèn, xem lại: (a) `status` có phải `draft`
không, (b) alias có trùng với prompt thật không, (c) từ khoá nội dung có phủ đủ không.

### "Train" nó thế nào?

Không có huấn luyện model — chỉ có **hai vòng phản hồi thực dụng**:

**1. Biết workflow nào thực sự giúp.** Mỗi lần chèn đều được ghi vào `kb_retrieval_log`, rồi
kết quả task được ghi ngược lại sau khi chạy xong (tab Knowledge → *Lịch sử dùng*). Workflow
được chèn nhiều mà task vẫn KHÔNG ĐẠT ⇒ vấn đề nằm ở **nội dung** workflow (viết lại), không
phải thiếu kiến thức.

**2. Xem agent nghĩ gì ở từng bước.** Mỗi thẻ bước trong tab **Các bước** có thêm dòng 🧠 *lý do
agent chọn hành động đó*. Với app dùng nút icon (không có nhãn chữ), đây là thứ duy nhất giải
thích được nó đang cố làm gì — dùng nó để viết workflow cho đúng.

> Đừng kỳ vọng "train" thay được prompt tốt: console không sửa được model. Nó chỉ giúp đại
> vương **đưa đúng ngữ cảnh vào prompt** và **biết workflow nào đang vô dụng**.


### Viết luật của app thế nào cho hiệu quả

Luật của app (modal, cảnh báo, hành vi bất ngờ) nên là một **workflow `status=verified`** có
`description` ngắn theo mẫu **cái này là gì → phải làm gì**, và alias đủ để khớp. Ví dụ tốt:

- `Modal "Chuyển nhà" hiện sau khi vào Trang chủ là gợi ý đổi nhà, KHÔNG phải lỗi → đóng bằng nút Huỷ rồi tiếp tục.`
- `Băng "2 thiết bị có bản cập nhật" là thông báo bình thường, không phải màn hình lỗi → bỏ qua.`
- `Mở app mà chưa đăng nhập thì app tự về màn hình Đăng nhập — đó là hành vi đúng, không phải FAILED.`

Tránh: mô tả dài dòng, hoặc luật mâu thuẫn với prompt (ví dụ cấm rời app nhưng prompt lại yêu
cầu mở Cài đặt).


```bash
# Prompt hoàn chỉnh mà máy tester nhận — đúng endpoint mà ARTEMIS_KB_API gọi.
curl -s -X POST http://127.0.0.1:8787/api/knowledge/context \
  -H 'Content-Type: application/json' -H 'Authorization: Bearer 21092003' \
  -d '{"goal":"kiểm tra nút đăng xuất","app_package":"com.iot.hunonic"}'
# → {"prompt":"kiểm tra nút đăng xuất\n\n[KIẾN THỨC VỀ CHỨC NĂNG …]","found":true,…}
```


## Ngân sách

Proxy ghi nhận `usage` từng request và chặn khi vượt:

| Giới hạn | Mặc định |
| --- | --- |
| Request / task | 40 |
| Output token / request | 768 |
| Chi phí / task | 2.000đ |
| Chi phí / ngày | 6.000đ |
| Thời gian / task | 480s |

Đổi bằng biến môi trường khi chạy console, ví dụ:

```bash
BUDGET_MAX_TASK_COST_VND=1000 BUDGET_MAX_REQUESTS=25 \
BUDGET_DAILY_COST_VND=4000 sh apps/test_console/run.sh
```

Khi chạm hạn mức: proxy trả `429 budget_exceeded`, task tự dừng, badge ngân sách
đỏ. Chi tiêu ngày lưu ở `data/budget.json`. Xem nhanh:
`curl -s http://127.0.0.1:8787/api/budget`.

### Hạn mức 40 request là quá thấp cho `--profile pro`

Đo thật (2026-10-26, app Hunonic, `Test đăng xuất`, profile `pro`): một task tốn
**41 request / ~539k prompt token** (Operator gửi kèm ảnh nên prompt rất lớn). Hạn mức
mặc định `BUDGET_MAX_REQUESTS=40` hết **đúng lúc final check**, nên Checker không chấm
được gì và kết quả là `inconclusive`.

Hệ thống xử lý đúng (báo `inconclusive`, không báo Pass sai), nhưng nếu muốn task Pro có
phán quyết thật thì nâng hạn mức:

```bash
BUDGET_MAX_REQUESTS=120 BUDGET_MAX_TASK_SECONDS=1200 \
BUDGET_MAX_TASK_COST_VND=8000 sh apps/test_console/run.sh
```

Lưu ý: proxy đếm request **theo tiến trình console**, không theo task. Chạy `artemis run`
trực tiếp từ CLI (không qua console) thì bộ đếm **không được reset** giữa các lần chạy —
nên hạn mức có thể cạn sau vài lần chạy CLI liên tiếp.

## Đổi model theo từng chức năng (không sửa config)

`config/artemis.jsonc` đọc tên model **và provider** của mỗi node từ `.env`:

```env
# Quy ước:
#   <MODEL>_<CHỨC NĂNG>            = id model
#   <MODEL>_<CHỨC NĂNG>_PROVIDER   = "openai" (Vilao, qua proxy, có chặn ngân sách)
#                                    hoặc "google" (key Gemini, free tier, KHÔNG qua proxy)

# ĐANG BẬT: model ER cho object_detector (đã kiểm chứng chạy được + toạ độ chính xác)
GEMINIER_OBJECT_DETECTOR_PROVIDER=google
GEMINIER_OBJECT_DETECTOR=gemini-robotics-er-2-preview

# Bật khi cần (free tier có sẵn): gemini-3.5-flash / 3.6 / 3.7 / 3.8 / 3.1-pro-preview
# GEMINI35FLASH_OPERATOR_PROVIDER=google
# GEMINI35FLASH_OPERATOR=gemini-3.5-flash
# GEMINI35FLASH_EXPLORER_PROVIDER=google
# GEMINI35FLASH_EXPLORER=gemini-3.5-flash
# GEMINI35FLASH_PLANNER_PROVIDER=google
# GEMINI35FLASH_PLANNER=gemini-3.5-flash
# GEMINI35FLASH_CHECKER_PROVIDER=google
# GEMINI35FLASH_CHECKER=gemini-3.5-flash

# Việc phụ (chỉ đọc chữ): giữ model rẻ
# GEMINI35FLASHLITE_HOPPER=ts/gemini-3.5-flash-lite
# GEMINI35FLASHLITE_SUMMARIZER=ts/gemini-3.5-flash-lite
```

Đổi model/provider cho một chức năng = **sửa đúng 1–2 dòng `.env`** rồi restart daemon
(`lsof -ti tcp:8000 | xargs -r kill`). Không khai biến nào thì giữ nguyên giá trị trong
`artemis.jsonc`.

| Chức năng | Node | Cần khả năng | Model nên dùng |
| --- | --- | --- | --- |
| Vòng lặp chính (Flash + Pro) | `operator`, `default` | vision + tool calling + quyết định | model **mạnh nhất** có |
| Nhìn màn hình tìm phần tử | `explorer` (pro/ultra) | vision + grounding | model mạnh |
| **Toạ độ chính xác** | `object_detector` | **Gemini ER** | `gemini-robotics-er-2-preview` ✅ đang dùng |
| Lập kế hoạch / kiểm tra | `planner`, `checker` | reasoning | model mạnh |
| Tìm package, nén lịch sử | `hopper`, `summarizer`, `chunking` | chỉ đọc chữ | model **rẻ** |

**Vì sao chia theo chức năng chứ không đổi hết sang model mạnh:** chỉ 2–3 node thật sự cần
model mạnh (nhìn màn hình + quyết định). Các node còn lại chỉ đọc chữ.

### Đã kiểm chứng `gemini-robotics-er-2-preview` trên free tier

```
GET  /v1beta/models                     → HTTP 200, 50 model, có gemini-robotics-er-2-preview
POST .../gemini-robotics-er-2-preview:generateContent (có ảnh chụp màn hình thật)
     → HTTP 200, serviceTier=standard
     → [{"point": [290, 500], "label": "Cửa cuốn"}]   ← đúng định dạng ARTEMIS cần
```

Đo lại bằng **đúng prompt của ARTEMIS** (`object_detector.json`: template + instructions) trên
ảnh `720×1600`, toạ độ quy về pixel để đối chiếu:

| Model | Kết quả | Pixel | Thời gian |
| --- | --- | --- | --- |
| `gemini-robotics-er-2-preview` | `{"point": [290, 500]}` | (360, 464) | 2.7s |
| `gemini-3.5-flash-lite` (Google trực tiếp) | `{"point": [304, 528]}` | (380, 486) | 2.8s |

Cả hai điểm đều rơi **trong thẻ "Cửa cuốn"** (thẻ trải khoảng x 252–475, y 355–570) nên bấm
đúng. Lưu ý: model ER chỉ trả `box_2d` khi hỏi bằng prompt tự chế; gặp prompt thật (có
`instructions` đòi `point`) thì nó trả `point` — code không cần xử lý `box_2d`.

Node `object_detector` trước đây chạy `flash-lite` dù chính comment trong `artemis.jsonc` ghi nó
**phải** dùng model ER, nếu không sẽ bấm sai toạ độ. Fallback trong config tự về Vilao nên nếu
free tier hết hạn mức thì task không gãy.

> ⚠️ **`thinking_level` chỉ có tác dụng khi provider là `google`.** Với provider `openai` (Vilao)
> `artemis/llm/router.py` bỏ qua nó im lặng. Đổi sang `google` thì request **không đi qua proxy
> console** nên mất phần chặn ngân sách (bù lại free tier không tính tiền).
>
> ⚠️ **Cổng free tier có hạn mức rất chặt** — xem mục dưới.

### Hạn mức thật của free tier (đo trên key của console)

Google **đã bỏ bảng hạn mức free tier** khỏi tài liệu; trang rate-limits giờ chỉ nói "xem trong
AI Studio". Nên con số dưới đây là **đo trực tiếp**: gửi request liên tiếp cho tới khi bị 429,
rồi đọc chính body lỗi (429 có kèm `QuotaFailure` ghi rõ tên quota và giá trị).

```
model=gemini-robotics-er-2-preview
gửi liên tiếp: 7 request OK trong 7.7s
lỗi đầu tiên ở request #8: HTTP 429 RESOURCE_EXHAUSTED
  quotaId : GenerateRequestsPerMinutePerProjectPerModel-FreeTier
  quotaVal: 5
  retryDelay: 58s

model=gemini-3.5-flash
gửi liên tiếp: 8 request OK trong 32.3s
lỗi đầu tiên ở request #9: HTTP 429
  quotaId : GenerateRequestsPerMinutePerProjectPerModel-FreeTier
  quotaVal: 5
  retryDelay: 35s
```

| Model | RPM | RPD (mỗi ngày) |
| --- | --- | --- |
| `gemini-3.5-flash`, `3.6-flash`, `3.7-flash`, `3.8-flash` | 5 | **20** |
| `gemini-robotics-er-2-preview` | 5 | ~20 |
| `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite` | **15** | **500** |
| `gemini-embedding-2` | 100 | 1000 |

Bốn điều quan trọng:

1. **Hạn mức tính theo project, không theo key.** Tạo thêm key trong cùng project không tăng gì.
   RPD reset lúc **nửa đêm giờ Thái Bình Dương**, không phải giờ Việt Nam.
2. **20 request/ngày là rất ít.** Một task ARTEMIS tốn 15–25 lượt gọi LLM ⇒ free tier Flash chỉ
   chạy được **1–2 task/ngày**. Vì vậy **không được** trỏ vòng lặp chính sang `google`.
3. **`object_detector` = 1 request cho mỗi nhãn cần tìm** (chưa tính retry khi parse lỗi). Hết
   20 request thì `classify_failure` xếp 429 vào `RATE_LIMIT` (retryable + should_fallback) nên
   ARTEMIS tự chờ rồi **rơi về Vilao** `ts/gemini-3.5-flash-lite` — task không gãy nhưng toạ độ
   kém chính xác hơn.
4. **`_DETECTOR_SEMAPHORE = 6`** cho phép 6 request detector song song, trong khi trần là 5/phút
   ⇒ gọi nhiều nhãn một lúc là chạm 429 ngay. Muốn tránh thì hỏi ít nhãn mỗi lượt.

**Chọn model cho `object_detector`:**

- `gemini-robotics-er-2-preview`: chính xác nhất, nhưng chỉ **5 RPM / ~20 request ngày**.
- `gemini-3.5-flash-lite` (Google trực tiếp): **15 RPM / 500 request ngày — gấp 25 lần**; đo trên
  cùng ảnh thì điểm rơi vẫn đúng thẻ. Đây là lựa chọn nên cân nhắc nếu chạy nhiều task mỗi ngày.
  (Lưu ý: `ts/gemini-3.5-flash-lite` **qua Vilao** là serving khác, trước đây từng trả sai toạ độ.)
- Muốn hết trần: **bật billing** cho project trong AI Studio → lên Tier 1, hạn mức tăng mạnh.

> ⚠️ **Free tier dùng nội dung để cải thiện sản phẩm của Google.** Ảnh chụp màn hình app của
> khách hàng sẽ bị gửi đi và có thể được dùng cho huấn luyện. Cân nhắc trước khi dùng cho dữ
> liệu khách.

## Khắc phục sự cố

## Khắc phục sự cố

### Task "treo" ở bước 0, app không được mở (đã sửa ở 0.3.2)

Triệu chứng: prompt kiểu *"tắt app đi và vào lại rồi đăng nhập"* → console hiện
**Đang chạy** mãi, chỉ có một thẻ bước nhãn *"Hành động"*, không thêm bước nào, cuối
cùng bị watchdog cắt. Nhìn ảnh chụp thì app đã bị đóng nhưng không được mở lại.

Nguyên nhân gốc (đọc từ `traces/<session>/stdout.log`):

```
Executing Flash tool: manage_app({'app_name': 'Hunonic', 'action': 'launch'})
Starting Hopper Agent (use_fallback=False)
LLM Error: 1 validation error for HopperOutput
  Invalid JSON: expected value at line 1 column 1
  [input_value='```json\n{\n  "found": f...ing to rule 4."\n}\n```']
  Pausing execution to wait for resume signal...     ← treo ở đây
```

Agent gọi `manage_app(action="launch")` → khởi động sub-agent **Hopper** để tìm
package. Model trả JSON **bọc trong ```` ```json ````**, `with_structured_output`
parse lỗi; lỗi đó bị xếp vào nhóm *retry được* nên ARTEMIS retry rồi **pause chờ
resume** — mặc định `LLM_PAUSE_TIMEOUT_SECONDS=900` (**15 phút**), nên tester thấy
như treo hẳn.

Đã xử lý 2 lớp:

- `artemis/services/llm.py` → `_TolerantStructuredRunnable`: bọc chain structured-output
  và cứu lại JSON bọc fence qua `parse_structured`, lấy text từ **hai** nguồn — kết quả
  `include_raw` và `errors()[*]["input"]` của chính `ValidationError` (nhánh
  `method="json_schema"` mặc định của langchain-openai ≥ 0.3 ném lỗi kể cả khi
  `include_raw=True`). Vì cứu từ lỗi nên **không tốn thêm lượt gọi LLM nào**.
  Trong log sẽ thấy: `Recovered HopperOutput from a strict-parser error (168 chars).`
- `.env` → `LLM_PAUSE_TIMEOUT_SECONDS=120`: nếu còn rơi vào pause thì bỏ cuộc sau
  2 phút thay vì 15 phút.

Kết quả đo trên máy thật, cùng một prompt *"tắt app Cài đặt đi rồi mở lại, cho biết
mức pin"*:

| | Trước | Sau |
| --- | --- | --- |
| `Pausing execution` trong trace | 1 | **0** |
| App được mở lại | không | **có** (`manage_app(action='launch')`) |
| Số bước / kết luận | 0–1, bị cắt | **5 bước, ĐẠT**, 55s, 70đ |

### App kẹt ở màn hình splash (logo Hunonic) mãi không vào

Triệu chứng: app đã cài, đã mở, nhưng cứ ở màn hình logo. Agent không biết nên
`wait_for_delay` liên tục rồi chạm lung tung — trông y như "chạy linh tinh".

Nguyên nhân: app **React Native bản debug** (build `999999`) không nhúng JS bundle, phải tải
từ Metro qua `adb reverse`. Khi Metro tắt **hoặc** `adb reverse` mất (rút/cắm cáp, `adb
kill-server`, restart máy), app kẹt splash vĩnh viễn.

```bash
# kiểm tra
adb reverse --list                       # phải có: UsbFfs tcp:8081 tcp:8081
lsof -ti tcp:8081                        # phải có tiến trình Metro

# sửa
cd /path/to/your-app && yarn start       # giữ terminal này chạy suốt lúc test
adb reverse tcp:8081 tcp:8081
adb shell am force-stop com.iot.hunonic  # mở lại app sau khi Metro lên
```

Console nay tự cảnh báo (toast đỏ + dòng `⚠` trong log task) khi thiếu `adb reverse`.
Đổi cổng: `CONSOLE_RN_METRO_PORT=8082`; tắt kiểm tra: `CONSOLE_RN_METRO_PORT=0`.

### Agent "chạy linh tinh": bấm HOME, mở app khác, chạm lung tung

Đây là chuyện của **model**, nhưng ARTEMIS để lọt một lỗ hổng khiến nó tệ hơn:
`--locked-app <package>` **chỉ** đảm bảo app đó ở foreground **lúc bắt đầu** — nó không
ràng buộc hành động sau đó, và **agent không được thông báo** là mình bị khoá vào app nào.
Nên khi prompt không nêu tên app, agent có thể tự `manage_app(stop, "App khác")`, bấm HOME,
mở danh sách ứng dụng, vuốt qua lại rồi chạm bừa.

Console nay chèn khối `[APP ĐANG TEST]` vào prompt (chứa package + các điều cấm). Đo trên
cùng một prompt:

| | Trước | Sau |
| --- | --- | --- |
| Hành động | `search_history` → `manage_app(stop, "Tuya Smart")` → `HOME` → `APP_SWITCH` → vuốt ×6 → chạm "Zing MP3" → nhập ô search… (20 lượt, bị cắt) | `manage_app(stop, com.iot.hunonic)` → `manage_app(launch, com.iot.hunonic)` → chờ (**3 lượt, xong**) |

Muốn tắt: `CONSOLE_APP_LOCK_HINT=0 sh apps/test_console/run.sh`.

Ba yếu tố còn lại khiến agent đi lạc, **không sửa được ở console**:

1. **Model**: Vilao hiện chỉ có **một** model là `ts/gemini-3.5-flash-lite` (bậc rẻ nhất).
   Muốn khá hơn phải đổi nhà cung cấp (OpenAI/Gemini/OpenRouter) hoặc dùng model local.
2. **Chế độ Flash** (phản ứng, không có planner/checker). Việc nhiều bước nên chạy **Pro**.
3. **Prompt mơ hồ** — luôn nêu rõ: làm gì, thành công khi nào, tối đa bao nhiêu bước.

### Tab "Các bước" trống, hoặc hiện bước của lần chạy trước

Triệu chứng: bấm **Chạy test** xong nhưng tab **Các bước** ghi *"chưa có bước thao tác nào"*,
hoặc chạy lại cùng một prompt thì thấy y nguyên bước của lần trước.

Nguyên nhân (đã sửa ở 0.3.2): console **đoán** session theo nội dung prompt và cache bản đoán,
nên khi stdout in ra session thật thì bị bỏ qua → UI đọc nhầm session của lần chạy trước.
Nay session đọc từ log của chính lần chạy là nguồn chính xác.

Nếu vẫn trống: mở **Log kỹ thuật** tìm dòng `Session:` và kiểm tra
`traces/<session>/stdout.log` có tồn tại không. Task chỉ quan sát (không chạm/vuốt) thì xem
khối **Diễn biến của agent** — nó liệt kê lượt, thao tác, lỗi, cảnh báo.

### Task chạy nhưng app không mở ("app không chạy")

Nguyên nhân hay gặp nhất: **ô Package đang khoá sai app**. Mở **Log kỹ thuật** và xem
dòng `$ ... --locked-app <package>`; nếu package đó không có trên máy thì ARTEMIS thử
3 lần × 15s rồi bỏ cuộc:

```
❌ Timeout waiting for com.example.app to load after 15s. Current foreground: com.iot.hunonic
❌ Failed to launch com.example.app after 3 attempts
```

Kiểm tra bằng tay:

```bash
adb shell pm list packages | grep -i hunonic                                # app có cài không
adb shell monkey -p com.iot.hunonic -c android.intent.category.LAUNCHER 1   # mở thử xem có lên không
```

Console nay chặn sẵn trường hợp package sai (báo `400` kèm gợi ý), nên nếu app **đã
cài** mà vẫn không mở thì là chuyện khác: app React Native bản debug cần Metro +
`adb reverse` (xem mục **Cài app lên điện thoại** ở trên), hoặc app tự crash — khi đó
đọc `adb logcat | grep -i hunonic`.

### Xem log khi task lỗi

| Cần xem | Ở đâu |
| --- | --- |
| Lệnh đã chạy + exit code | `data/logs/<ts>_<test>_<id>.log`, hoặc nút **Xem log kỹ thuật** trên card kết luận |
| Traceback thật của task | `~/Library/Application Support/artemis/logs/daemon-8000.log` |
| Reasoning từng bước | `traces/<session_id>/stdout.log` (session id hiện trên card kết luận) |
| Lỗi cũ đã đóng gói | `traces/web_<ts>_<id>_FAIL_<iso>/` |

Mẹo: `grep -n 'Traceback\|❌\|Pausing execution' traces/<session_id>/stdout.log`

### Task báo KHÔNG ĐẠT nhưng agent làm đúng

Xem `summary.issues` trên card: nếu là *"đã chạy quá Ns"* thì task bị watchdog cắt
do quá thời gian, không phải agent sai — tăng **Giới hạn thời gian** rồi chạy lại.

## Ghi chú

- Server chỉ bind `127.0.0.1`, không lộ ra mạng LAN.
- Key LLM không bao giờ gửi xuống trình duyệt; UI chỉ hiển thị 4 ký tự cuối.
- Khoá truy cập cũng không bao giờ trả về qua API: `/api/health` chỉ báo `required` + 4 ký tự cuối.
- Mỗi task chạy `artemis run ... --locked-app <package> --profile <flash|pro>`.
- Log lưu tại `apps/test_console/data/logs/`, lịch sử tại `data/tasks.json`.
- Task gửi ảnh màn hình + XML UI lên nhà cung cấp LLM: dùng tài khoản test.
