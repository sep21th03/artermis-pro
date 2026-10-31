# Workflow RAG — knowledge base cho HunonicTest

Cho agent biết trước **đường dẫn thao tác** của từng chức năng, thay vì tự mò.
Người dùng nhập `Test đăng xuất`, hệ thống truy xuất workflow `auth.logout` và
đưa các bước vào Planner.

## Cài đặt

```bash
brew install pgvector                 # extension cho PostgreSQL
createdb artemis_kb
uv pip install 'psycopg[binary]' pgvector     # hoặc: pip install -e '.[knowledge]'
psql -d artemis_kb -c 'CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pg_trgm;'
```

Thêm vào `.env`:

```bash
ARTEMIS_KB_DSN=postgresql://<user>@localhost:5432/artemis_kb
ARTEMIS_KB_EMBEDDING_MODEL=gemini-embedding-2
```

Schema tự tạo ở lần khởi động đầu tiên (`schema.sql`, idempotent).

**Bỏ trống `ARTEMIS_KB_DSN` = tắt tính năng.** HunonicTest chạy y như trước, không lỗi.

## Kiến trúc

```
Goal ──► KnowledgeService.retrieve(query, app_id, version_id)
              │
              ├─ kênh exact    : id / alias khớp
              ├─ kênh keyword  : full-text search trên bản đã chuẩn hoá
              └─ kênh vector   : cosine trên pgvector
              │
              ▼  Reciprocal Rank Fusion (k=60)
         WorkflowHit[] + cờ `trusted`
              │
              ▼  build_context()  →  khối văn bản chèn vào prompt Planner
```

Mã nguồn:

| Tệp | Việc |
|---|---|
| `models.py` | `Workflow`, `WorkflowStep`, `WorkflowHit`, `RetrievalResult` — biên validate |
| `schema.sql` | 11 bảng, migration idempotent |
| `text.py` | chuẩn hoá tiếng Việt, stopword, độ phủ token, che bí mật |
| `embeddings.py` | Gemini embedder (batch, `task_type`, tự hạ cấp khi model từ chối) |
| `retrieval.py` | RRF + quyết định `found` (hàm thuần, không DB, không mạng) |
| `context.py` | dựng khối văn bản cho Planner |
| `service.py` | cửa duy nhất; mọi lỗi → `degraded`, không bao giờ raise vào run |
| `stores/postgres.py` | PostgreSQL + pgvector |
| `api.py` | HTTP CRUD cho console |

## Mô hình tin cậy — phần quan trọng nhất

Không phải mọi "khớp" đều là bằng chứng. Ba kênh có độ tin cậy rất khác nhau:

* **`exact`** (khớp id hoặc alias) → **luôn tin**. Tester đã gọi đúng tên workflow.
* **`keyword`** → tin **chỉ khi độ phủ token nội dung ≥ `ARTEMIS_KB_KEYWORD_MIN_COVERAGE`**
  (mặc định 0.6). Kênh này xếp hạng bằng truy vấn OR nên một từ chung chung cũng khớp.
* **`vector`** → **không bao giờ tự nó đủ để kết luận**. Truy vấn nearest-neighbour luôn
  trả về kết quả, kể cả với yêu cầu chẳng liên quan gì.

Ngưỡng similarity **cố định không dùng được**: đo trên `gemini-embedding-2` với màn hình
thật, yêu cầu lạc đề vẫn đạt 0.55–0.65 còn câu diễn đạt lại đúng nghĩa đạt 0.59–0.73 —
hai phân bố **chồng lấn**, không cutoff nào tách được. Vì vậy:

* `found` do **kênh từ vựng** quyết định, không do vector.
* Mục chỉ khớp vector vẫn được hiển thị, nhưng dưới nhãn **"CÓ THỂ LIÊN QUAN — máy đoán"**,
  và **chỉ in tên + mô tả, KHÔNG in các bước**.

Lý do của quy tắc cuối: `chụp ảnh màn hình` chia sẻ từ `màn hình` với workflow đăng xuất.
Nếu bản đoán được in đủ bước, agent có thể đăng xuất tài khoản khi được yêu cầu chụp ảnh —
một hành động phá hoại do một gợi ý sai gây ra.

Khi không có gì khớp, khối văn bản **nói thẳng là không có hướng dẫn**, để agent tự khám phá
thay vì tự bịa quy trình.

## Đo đạc

`tests/integration/test_knowledge_pipeline.py` (23 test) là acceptance test; chạy:

```bash
pytest tests/integration/test_knowledge_pipeline.py -m integration -v
```

Bộ dữ liệu mẫu 3 workflow, truy vấn thật:

| Truy vấn | Kết quả | Kênh |
|---|---|---|
| `Test đăng xuất` | ✅ `auth.logout` | exact + keyword + vector |
| `thoát khỏi tài khoản đang dùng` | ✅ `auth.logout` | keyword + vector |
| `logout` | ✅ `auth.logout` | exact + keyword + vector |
| `chụp ảnh màn hình` | ⛔ không xác nhận, 1 mục "máy đoán" | keyword (phủ 0.5) + vector |

Chi phí mỗi lần truy xuất: **~0.5–0.8 s**, gần như toàn bộ là gọi embedding Gemini.
Hai kênh SQL chỉ ~10 ms.

## Nối vào HunonicTest (Phase 2)

```
run_automation(goal, locked_app_package)          third_party/mobile_use/main.py
   │  retrieve_for_run(goal, package)              ← trước khi agent khởi động
   │     resolve_app_id(package)  → app_id
   │     service.context_for(goal, app_id)         → RetrievalResult + khối văn bản
   ▼
attach(ctx, knowledge)                             gắn lên ArtemisContext (extra="allow")
   ▼
Agent._run_task → graph → PlannerNode
   │  context_text(self.ctx)                      đọc lại khối kiến thức
   │  build_planner_system_blocks(..., include_knowledge=bool(kb))
   │  render_planner_human(template, goal, kb)     Goal: ...\n\n[KIẾN THỨC...]
   ▼
Operator quan sát UI thật → thực hiện
   ▼
exit_settlement → RunOutcome → record_outcome(run_id, outcome)
```

**Vì sao đi qua `ArtemisContext` mà không thêm field vào `State`:** `State` là
`extra="forbid"` và bị manifest `artemis/graph/visibility.py` kiểm soát (có enforcer khi
`ARTEMIS_STRICT_STATE=1`), nên thêm field là đổi hợp đồng. `ArtemisContext` là
`extra="allow"` và Planner vốn đã giữ nó — gắn vào đó không đổi gì cả. Run không có kiến
thức thì không set attribute, và mọi chỗ đọc coi "thiếu" = "không có kiến thức".

**Chỉ kiến thức đã xác nhận mới vào Planner.** `KnowledgeContext.injected` đòi
`found=True`. Một kết quả chỉ khớp vector (máy đoán) không vào prompt: run phải hành xử y
như khi tắt RAG. Bản đoán vẫn xem được ở `POST /api/kb/retrieve` cho **người** đọc.

## Kiểm chứng đầu-cuối trên thiết bị thật (2026-10-26)

App `com.iot.hunonic`, thiết bị `R7AY30DH7GR`, `--profile pro`, goal `Test đăng xuất`:

| Bước | Bằng chứng |
|---|---|
| RAG truy xuất | `RAG: 1 workflow truy xuất (1 xác nhận) cho app hunonic (901ms)` |
| Planner **nhận** workflow | `Planner nhận knowledge context: 1475 ký tự, workflow=['auth.logout'], app=hunonic` |
| Operator làm theo | bước 2 chạm tab cuối (`android.widget.Button`); bước 3 chạm phần tử `target_text: "Đăng xuất"`, bounds `[28,1093,692,1198]` |
| Outcome ghi vào log | `kb_retrieval_log.outcome = {"task_status": "partial", "tests": {"inconclusive": 1, ...}}` |
| Checker | ⚠️ **không chấm được** — hết `BUDGET_MAX_REQUESTS=40` đúng lúc final check (`Checker transcript: 0 segment(s)`), hệ thống báo `inconclusive` chứ không báo Pass sai |

**Bài học về hạn mức:** một run Pro cho một chức năng tốn ~41 request / ~539k prompt token.
Muốn Checker có phán quyết thật thì phải nâng `BUDGET_MAX_REQUESTS` (mặc định 40 là quá
thấp cho Pro có check).

## Giao diện quản lý (Phase 3)

Tab **Knowledge** trong Mobile Test Console (`apps/test_console/static/index.html`):
danh sách app + workflow theo app, tìm kiếm, trình soạn workflow và bước, kiểm tra truy
xuất (in ra đúng khối văn bản sẽ vào Planner), xem kiến thức đã dùng trong các lần chạy,
và trạng thái embedding. Chi tiết cách dùng: `apps/test_console/README.md`.

UI là một file HTML viết tay nên `tests/unit/test_console_kb_ui.py` kiểm tra **tĩnh**:
mọi `$('id')` phải tồn tại, không id trùng, mọi đường dẫn `/api/kb/…` phải có route, và
script phải parse được bằng `node --check`.

## AI Prompt Preparation — `POST /api/knowledge/prepare`

`retrieve()` cho agent biết **kiến thức**; bước chuẩn bị biến kiến thức đó thành
**một prompt thực thi** mà tester đã xem và duyệt.

```
Prompt gốc
   │
   ├─ 1. Hiểu mục tiêu (1 lượt LLM)      tasks[] có điều kiện + final_goal
   │        mỗi việc phải bám vào câu của tester; định danh phải được giữ
   │
   ├─ 2. Truy xuất theo TỪNG việc        KnowledgeService.retrieve() × số việc
   │        (đúng service của RAG: RRF + trust policy + kb_retrieval_log)
   │
   ├─ 3. Phân loại vai trò               PRIMARY / SUPPORTING / IRRELEVANT
   │        vai trò quyết định *nội dung nào được phép vào prompt*
   │
   ├─ 4. Tổng hợp (1 lượt LLM)           LLM viết lại thành 5 mục cố định
   │
   ├─ 5. Kiểm tra chất lượng             10 tiêu chí bằng máy
   │
   └─ 6. Dự phòng nếu trượt              prompt gốc + kiến thức dựng bằng luật
                                          (cùng 5 mục) + status="fallback"
   │
   ▼  tester xem/sửa → Chạy              prompt_override + ARTEMIS_KB_PRESUPPLIED
```

Mã nguồn: `artemis/knowledge/intent.py` (hiểu mục tiêu + phân loại vai trò + dựng phần
kiến thức) và `artemis/knowledge/prepare.py` (điều phối, cache, hạn mức, kiểm tra chất
lượng) + endpoint trong `apps/test_console/server.py`.

### Vì sao phải hiểu mục tiêu trước

Lỗi đã gặp: `mở app và đăng nhập rồi chuyển sang nhà dled` được coi là **một** truy vấn,
nên mọi workflow trùng từ đều vào prompt như nhau:

| Workflow | Vì sao nó lọt vào | Hậu quả |
| --- | --- | --- |
| `home.switch_house_modal` | alias "chuyển nhà", "đổi nhà" | luật đóng modal bị viết thành **bước chuyển nhà** ("nhấn Huỷ") |
| `auth.switch_account` | alias "đăng nhập tài khoản" | mặc định là tài khoản **đã lưu sẵn** |
| `auth.logout` | alias "đăng nhập tài khoản khác" | đăng xuất xuất hiện dù tester không yêu cầu |

Không có lỗi nào ở tầng RRF: mọi hit đều chia sẻ từ ngữ với câu. Thiếu bước **hiểu câu là
ba việc có điều kiện** và thiếu khái niệm **workflow liên quan nhưng không phải mục tiêu**.

### Vai trò quyết định nội dung (chốt chặn cơ học)

| Vai trò | Cách xác định | Được vào prompt phần nào |
| --- | --- | --- |
| **PRIMARY** | tên workflow phủ hành động của việc (≥ `NAME_COVERAGE_FLOOR`) **và** hit có bằng chứng từ vựng (`trusted`) | mục `KIẾN THỨC ỨNG DỤNG`: mô tả, điều kiện phải kiểm tra, cảnh báo, tiêu chí |
| **SUPPORTING** | workflow luật/modal (tag `rule`/`modal`), hoặc chỉ mô tả/alias phủ hành động | mục `BỐI CẢNH / CẢNH BÁO`: **chỉ tên + mô tả một dòng** |
| **IRRELEVANT** | không phủ hành động nào | không vào prompt; báo trong `excluded_workflows` |

Vì tiêu chí và các bước của workflow SUPPORTING **không bao giờ** được gửi cho LLM, một
luật không thể trở thành việc phải làm — kể cả khi LLM muốn. Thêm một chốt thứ hai ở tầng
kiểm tra: nhãn UI trong ngoặc kép của workflow phụ (ví dụ `'Huỷ'`) không được xuất hiện
trong hai mục chỉ dẫn, nếu không thì bản LLM bị từ chối và dự phòng được dùng.

### Năm mục của phần kiến thức

```
KẾ HOẠCH:                 các việc theo thứ tự, kèm điều kiện thực hiện
KIẾN THỨC ỨNG DỤNG:       cách làm của workflow THỰC HIỆN (không có bước cứng)
BỐI CẢNH / CẢNH BÁO:      luật/modal — nêu rõ KHÔNG phải mục tiêu, không phải bước
KB CHƯA CÓ HƯỚNG DẪN:     việc KB không có workflow: tự khám phá UI, không tự suy diễn
TIÊU CHÍ XÁC MINH:        mục tiêu cuối + tiêu chí của workflow THỰC HIỆN
```

Khi hết hạn mức ký tự, thứ tự bị cắt là: bối cảnh → kiến thức chi tiết. Kế hoạch, khoảng
trống và tiêu chí luôn ở lại — mất chúng nguy hiểm hơn mất một dòng mô tả.

### Trạng thái trả về

| `status` | Nghĩa | LLM có được gọi? | `ready` |
| --- | --- | --- | --- |
| `llm` | Tổng hợp thành công, qua đủ 10 kiểm tra | có (2 lượt) | `true` |
| `fallback` | LLM lỗi / quá hạn / không đạt kiểm tra ⇒ bản dựng bằng luật | có (nếu lỗi từ LLM) | `true` |
| `no_knowledge` | KB không có workflow nào phục vụ các việc ⇒ giữ nguyên prompt gốc | **không** | `false` |
| `degraded` | Không truy cập được KB ⇒ giữ nguyên prompt gốc | **không** | `false` |

`ready` nghĩa là *bản trả về đã được kiểm tra chất lượng và dùng được ngay*; `false` chỉ
nói rằng hệ thống không thêm gì được, không phải prompt gốc hỏng — tester vẫn đọc, sửa và
chạy bình thường.

Hai dòng cuối là lý do không có lượt gọi LLM nào khi KB không có gì để nói: tổng hợp một
khoảng trống vừa tốn tiền vừa mời model bịa. `knowledge_gaps` vẫn được trả về trong hai
trường hợp đó, nên tester thấy ngay *việc nào* thiếu kiến thức.

### Kiểm tra chất lượng (10 tiêu chí)

`original_preserved`, `identifiers_preserved`, `no_invented_data`, `no_hard_ui`,
`knowledge_grounded`, `sections_ok`, `tasks_kept` (soi **phần chỉ dẫn**, vì prompt gốc luôn
chứa đủ từ của mọi việc), `primary_knowledge_kept`, `supporting_not_a_step`, `size_ok`.
Trượt một tiêu chí ⇒ không dùng bản LLM, và lý do được ghi vào `fallback_reason`.

### Cache, hạn mức, đo đạc

* **Cache tra trước cả lượt phân tích mục tiêu.** Khoá là `(câu hỏi, app, profile)`; tính
  đúng đắn nằm ở chỗ fingerprint của các workflow đã dùng được **tính lại từ database** khi
  tra cứu (vài lượt đọc theo khoá chính, không embedding, không LLM). Sửa hoặc xoá một
  workflow trong số đó là cache tự vô hiệu.
* **Không cache bản fallback** — một lần LLM hỏng thoáng qua không được ghim tester vào bản
  suy giảm suốt TTL.
* **Rate limit** theo khoá truy cập (hoặc IP ở máy dev), mặc định 30 lần/phút, trả `429`
  kèm `Retry-After`. Chi phí của bước chuẩn bị vào hạn mức **ngày** của console
  (`Budget.record_prepare`), không vào hạn mức của task.
* **`metrics`** trả về `intent_ms`, `retrieval_ms`, `synthesis_ms`, `total_ms`, `llm_calls`,
  `usage` (token), `queries`, `plan_source`, `primary/supporting/excluded/gaps` — đủ để biết
  chi phí và độ trễ của từng giai đoạn.

### Phân công

RAG cung cấp kiến thức. LLM hiểu mục tiêu và tổng hợp. Tester duyệt. Agent thao tác và xác
minh trên UI thực tế — và **không** truy xuất KB lần thứ hai: console đặt
`ARTEMIS_KB_PRESUPPLIED=1` khi chạy prompt đã duyệt, và `retrieve_for_run()` trả về ngay
"không có kiến thức" để khối đó không vào prompt hai lần.


## Chưa làm (Phase 4)

* **Học workflow từ phiên chạy thành công**: thu thập action history của một run ĐẠT, tổng
  hợp thành workflow có cấu trúc, đề xuất tên/mô tả/alias, **chờ người duyệt** rồi mới embed.
  Chưa làm. Hiện đã có sẵn nền: `kb_workflow_revision` giữ lịch sử, và tab Knowledge cho
  phép sửa + duyệt thủ công.
* **Profile `flash` không dùng được RAG**: `flash` chạy `FlashRunner`, một vòng phản ứng
  đơn **không có Planner**, nên không có chỗ tiêu thụ khối kiến thức. `run_automation` in
  cảnh báo rõ khi gặp tình huống này. Muốn RAG có tác dụng thì dùng `--profile pro`.

## Biết trước, để không mất thời gian

* `gemini-embedding-2` **bỏ qua** `task_type` (vector query và document của cùng một
  đoạn text giống hệt nhau, cosine 1.0000). Tham số vẫn được truyền vì các model khác
  dùng nó thật.
* Cột `kb_workflow_embedding.embedding` là `vector` **không định độ rộng**, để nhiều model
  cùng tồn tại. Đổi lại là không tạo được index HNSW → tìm kiếm quét toàn bộ. Với vài nghìn
  workflow vẫn chỉ vài ms; nếu vượt ngưỡng đó thì cố định số chiều rồi thêm index.
* Stopword được so khớp trên **chữ còn dấu**. Bỏ dấu trước sẽ tạo từ đồng âm: `đăng`
  (đăng xuất) và `đang` (đang dùng) cùng thành `dang`, nên `đăng xuất` sẽ mất một nửa
  tín hiệu. Đây là lỗi đã từng xảy ra và có test hồi quy.
