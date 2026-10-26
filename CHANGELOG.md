# Changelog — ARTEMIS Pro

## [Unreleased]

### Changed

- **Bước chuẩn bị prompt nay *hiểu mục tiêu* trước khi truy xuất.** Trước đây cả câu của
  tester là **một** truy vấn, rồi top-K workflow được nối vào prompt như nhau. Với
  `mở app và đăng nhập rồi chuyển sang nhà dled` điều đó cho ra: modal 'Chuyển nhà' bị
  viết thành **bước chuyển nhà** ("nhấn Huỷ"), `auth.switch_account` bị coi là cách đăng
  nhập mặc định, và `auth.logout` lọt vào vì một alias đọc là "đăng nhập tài khoản khác" —
  trong khi việc chuyển nhà thật ra **không có workflow nào trong KB**.

  Pipeline mới: **hiểu mục tiêu (LLM) → truy xuất theo TỪNG việc → phân loại vai trò
  workflow → tổng hợp (LLM) → 10 kiểm tra chất lượng → dự phòng dựng bằng luật**.

  - `artemis/knowledge/intent.py` **(mới)** — tách câu thành các việc có điều kiện
    (`action`/`target`/`condition` + `final_goal`). Mỗi việc phải bám vào chính câu của
    tester (độ phủ token ≥ 0.6) nên không thể tự thêm "đăng xuất"; định danh (tên nhà, số
    điện thoại) phải được giữ nguyên, nếu không thì lùi về kế hoạch một việc.
  - **Truy xuất theo từng việc** qua đúng `KnowledgeService.retrieve()` của RAG — mỗi việc
    một dòng `kb_retrieval_log`.
  - **Vai trò quyết định nội dung**: PRIMARY (tên workflow phủ hành động **và** hit có
    bằng chứng từ vựng) vào mục kiến thức; SUPPORTING (luật/modal, hoặc chỉ trùng từ) chỉ
    còn **một dòng bối cảnh** — tiêu chí và bước của nó không bao giờ được gửi cho LLM, nên
    một luật không thể trở thành việc phải làm; IRRELEVANT bị loại và báo trong
    `excluded_workflows`.
  - **Khoảng trống được báo ra**: việc không có workflow THỰC HIỆN ⇒ `knowledge_gaps` +
    mục `KB CHƯA CÓ HƯỚNG DẪN` (tự khám phá UI, không tự suy diễn quy trình) thay vì để
    LLM bịa một quy trình.
  - **Enhanced Prompt có cấu trúc 5 mục**: `KẾ HOẠCH` / `KIẾN THỨC ỨNG DỤNG` /
    `BỐI CẢNH / CẢNH BÁO` / `KB CHƯA CÓ HƯỚNG DẪN` / `TIÊU CHÍ XÁC MINH`. Hết hạn mức thì
    cắt bối cảnh và kiến thức chi tiết trước; kế hoạch, khoảng trống và tiêu chí luôn ở lại.
  - **Kiểm tra chất lượng từ 9 lên 10 tiêu chí**, thêm `sections_ok`, `tasks_kept` (soi
    *phần chỉ dẫn*, vì prompt gốc luôn chứa đủ từ của mọi việc), `supporting_not_a_step`
    (nhãn UI trong ngoặc kép của workflow phụ — ví dụ `'Huỷ'` — không được xuất hiện trong
    phần chỉ dẫn); `preconditions_kept`/`success_criteria_kept`/`warnings_kept` gộp thành
    `primary_knowledge_kept` và chỉ áp cho workflow THỰC HIỆN.
  - **Cache tra trước cả lượt phân tích mục tiêu**: khoá `(câu hỏi, app, profile)`, xác
    nhận lại bằng fingerprint của các workflow đã dùng **tính từ database** khi tra cứu —
    lần lặp lại không tốn embedding, không tốn LLM.
  - **`metrics` theo từng giai đoạn**: `intent_ms`, `retrieval_ms`, `synthesis_ms`,
    `total_ms`, `llm_calls`, `usage`, `queries`, `plan_source`, số primary/supporting/
    excluded/gaps.
  - Hợp đồng trả về thêm `tasks`, `plan`, `excluded_workflows`, `knowledge_gaps`,
    `warnings`, `metrics`, `ready`; `matched_workflows` mỗi dòng có `role`, `role_reason`,
    `evidence`. Web UI hiển thị các thông tin này ngay dưới ô prompt.
  - Cấu hình mới: `ARTEMIS_PREPARE_INTENT_ENABLED`, `ARTEMIS_PREPARE_MAX_TASKS`,
    `ARTEMIS_PREPARE_INTENT_MAX_TOKENS`, `ARTEMIS_PREPARE_INTENT_TIMEOUT`,
    `ARTEMIS_PREPARE_NAME_COVERAGE_FLOOR`, `ARTEMIS_PREPARE_INTENT_COVERAGE_FLOOR`,
    `ARTEMIS_PREPARE_CONFLICT_FLOOR`.
  - Test: `tests/unit/test_knowledge_intent.py` (27 ca), `tests/unit/test_prepare_cases.py`
    (34 ca — đúng sáu ca bắt buộc), `tests/unit/test_knowledge_prepare.py` (39 ca cơ chế),
    `tests/unit/test_console_prepare_api.py` (36 ca), `tests/unit/test_console_prepare_ui.py`
    (20 ca), `tests/integration/test_prepare_pipeline.py` (9 ca). Không hardcode từ khoá
    theo miền nghiệp vụ: từ vựng duy nhất mà hệ thống biết là *schema của KB* (một workflow
    gắn tag `rule`/`modal` là luật) và chính câu của tester.

### Added

- **AI Prompt Preparation — bước chuẩn bị prompt tập trung trên máy chủ.** Trước đây
  console nối **nguyên khối workflow** vào prompt của tester: prompt dài, đầy hướng dẫn
  UI cứng, và agent đọc một kịch bản thay vì một mục tiêu. Nay có
  `POST /api/knowledge/prepare` (`CONSOLE_ROLE=server` và `all`, **không** đăng ký ở
  `edge`): nhận prompt gốc + `app_id`/`app_package` + `profile`, truy xuất workflow qua
  **đúng** `KnowledgeService.retrieve` của RAG (một lần, có ghi `kb_retrieval_log`), gọi
  LLM **qua chính AI proxy hiện có** để tổng hợp thành một Enhanced Prompt ngắn, rồi trả
  về `original_prompt` / `enhanced_prompt` / `matched_workflows` / `source` / `status`.
  Không tạo RAG thứ hai, không đổi kiến trúc server–edge.

  Bất biến được cưỡng chế bằng máy, không bằng lời hứa: Enhanced Prompt **luôn** bắt đầu
  bằng prompt gốc nguyên văn (LLM chỉ viết phần kiến thức phía sau), 9 kiểm tra chất
  lượng chạy trên bản LLM — `original_preserved`, `identifiers_preserved`,
  `no_invented_data`, `no_hard_ui`, `knowledge_grounded`, `preconditions_kept`,
  `success_criteria_kept`, `warnings_kept`, `size_ok`. Trượt một kiểm tra ⇒ **fallback**:
  prompt gốc + kiến thức rút gọn bằng logic xác định (`condense_knowledge`), kèm
  `status="fallback"` và lý do cụ thể để tester biết. Không có workflow nào phù hợp thì
  **không gọi LLM** (`status="no_knowledge"`).

  Không sao chép workflow (bước không bao giờ vào prompt; chỉ mô tả, điều kiện trước,
  cảnh báo, tiêu chí hoàn thành) và không toạ độ (`sanitize_ui_instructions` xoá bounds
  `[x,y,x,y]`, cặp `(x, y)`, `px/dp`, chữ "toạ độ"). Có timeout, cache **theo phiên bản
  KB** (sửa workflow là tự vô hiệu; không cache bản fallback), trần token/ký tự, xác thực
  bằng `CONSOLE_ACCESS_KEY` ở vai trò `server`, và rate limit trả `429` kèm `Retry-After`.

- **Web UI: nút ⚡ Chuẩn bị prompt trong Mobile Test Console.** Tester nhập prompt → bấm
  *Chuẩn bị* → thấy Enhanced Prompt trong ô sửa được kèm một dòng trạng thái (LLM hay
  fallback, workflow nào, `app_id`, phiên bản KB, cache, kiểm tra nào trượt, lý do) →
  *✔ Dùng bản này* → *Chạy*. Ở vai trò `edge` (không có route) nút bấm báo đúng địa chỉ
  máy chủ, lấy từ trường `kb_api` mới của `GET /api/health`.

- **Flash và Pro chạy đúng prompt đã duyệt, không truy xuất lần thứ hai.**
  `prompt_override` đi thẳng vào CLI và console đặt `ARTEMIS_KB_PRESUPPLIED=1`, nên
  `retrieve_for_run()` trả về ngay "không có kiến thức" thay vì chèn khối đó lần nữa —
  đúng cho cả `--profile flash` (vốn không có Planner) và `--profile pro`.

- `ARTEMIS_PREPARE_*` trong `.env` (bật/tắt, trần kiến thức, trần prompt, `max_tokens`,
  timeout, ngưỡng độ phủ, TTL/kích thước cache, rate limit, `top_k`). Chi phí lượt tổng
  hợp vào **hạn mức ngày** (`Budget.record_prepare`), không vào hạn mức của task.

### Fixed

- **Tên model trong `config/artemis.jsonc` được nội suy trước khi gọi LLM.** Trên VPS
  model là `${env:GEMINI35FLASHLITE_DEFAULT:-ts/gemini-3.5-flash-lite}`; hàm đọc config
  của console là regex nên trả về chuỗi **thô**, và gửi nguyên chuỗi đó làm tên model thì
  gateway trả 404 — trông như AI proxy hỏng. `resolve_model_name()` dùng đúng
  `artemis.config.core.interpolator` của ARTEMIS, nên bước chuẩn bị gọi đúng
  `ts/gemini-3.5-flash-lite`.

- Test: `tests/unit/test_knowledge_prepare.py` (44 ca), `tests/unit/test_console_prepare_api.py`
  (34 ca, gồm vai trò server/edge/all, xác thực, 429, fallback, cache, và lệnh CLI thật của
  Flash/Pro), `tests/unit/test_console_prepare_ui.py` (kiểm tra tĩnh giao diện),
  `tests/integration/test_prepare_pipeline.py` (luồng chuẩn bị → duyệt → sửa → chạy với
  truy xuất thật, store trong bộ nhớ).


## [0.4.9] — 2026-10-26

### Fixed

- **`flash.step_summarizer.enabled: false` bị bỏ qua ở mọi profile ngoài Flash.** Cờ này chỉ
  được `FlashRunner` tôn trọng; composition root ở `artemis/memory` đọc `.model` mà không hề
  nhìn `.enabled`, nên một lần chạy Pro vẫn dựng dịch vụ tóm tắt và trả tiền cho nó. Đo trên
  trace thật (`232d2382`): **15 bước × 4 lần thử = 60 lượt gọi vision, tất cả 404** — 45 lần
  `Retrying` + 15 lần `Giving up after 4 failed attempts`. Dịch vụ gọi **thẳng Google**
  (`get_google_llm`) với model `ts/gemini-3.5-flash-lite` mà endpoint đó không biết, nên mỗi
  lượt còn gửi kèm 2 ảnh chụp màn hình. Vì đi thẳng, nó **không bị proxy đếm, không vào log
  nginx, không tính vào ngân sách** — nhìn log VPS sẽ tưởng không có gì bất thường.

- **Console không hiển thị gì về việc agent đang làm.** `artemis run` giao việc cho Artemis
  Daemon rồi chỉ in vài dòng lịch trình: đo được **12 dòng cho cả một task**, không một dòng
  nào về lượt gọi LLM hay hành động. Output thật nằm ở `traces/<session>/stdout.log` và console
  chưa bao giờ đọc. Nay có luồng tail file đó (đọc từ đầu, theo kịp file đang lớn, đọc nốt phần
  còn lại sau khi tiến trình kết thúc) và ghi rõ nguồn. Đây là thứ khiến mọi thứ đi qua VPS —
  RAG, từng lượt LLM — trở nên vô hình với người test.

- **Agent tap cùng một chỗ 10 lần liên tiếp mà không ai chặn.** Trace `232d2382`: tap
  `[135,631][586,670]` **10 lần**, màn hình không đổi, mỗi vòng 4-10 lượt LLM — cả lần chạy
  **47 lượt trong 2m35s**. Thêm chốt ở tầng thực thi: hành động `tap`/`long_press` lặp y hệt
  (toạ độ làm tròn 10px) 3 lần liên tiếp thì bị chặn kèm lý do để Operator đổi cách làm. Cuộn
  và vuốt không bị chặn (lặp lại là bình thường) nhưng vẫn được ghi vào lịch sử, nên một lần
  cuộn xen giữa sẽ cắt chuỗi lặp. Loại lỗi mới `ValidationErrorCategory.REPEAT_LOOP`.

- **Workflow `auth.logout` bắt agent cuộn trong khi mục đã hiện sẵn.** Đo trên máy thật
  (720x1600): mục `Đăng xuất` ở `[38,683][961,748]` — **73% chiều cao màn hình, đã hiện**;
  hàng ngay trên nó là `Đánh giá 5 sao` ở `[38,491][961,556]`. Bước `scroll` đứng riêng luôn
  chạy bất kể mục đã hiện, làm lệch hàng rồi tap nhầm `Đánh giá 5 sao`. Nay bỏ bước scroll
  riêng, gộp điều kiện vào bước tap, ghi rõ hàng cần tránh và ràng buộc "không tap lại cùng
  một chỗ quá 2 lần". Revision 6.

### Added

- **Xem và sửa prompt hoàn chỉnh trước khi chạy.** Nút *🔍 Xem prompt hoàn chỉnh* dưới ô prompt
  gọi API RAG, hiện đúng prompt agent sẽ nhận (prompt gốc + hướng dẫn workflow), cho sửa rồi
  bấm *Dùng bản này*. Khi đó console gửi `prompt_override` và đặt `ARTEMIS_KB_PRESUPPLIED=1`
  cho tiến trình con, để ARTEMIS không lấy kiến thức lần nữa và chèn hai lần. Sửa prompt gốc
  thì bản đã sửa bị bỏ (nếu không sẽ chạy nội dung của lần gõ trước).
- `/api/kb/status` trả thêm `database` (đã bỏ mật khẩu) và tab Knowledge hiện nó. DSN của máy
  dev trỏ `127.0.0.1:5433` — cổng SSH tunnel sang VPS — nên rất dễ tưởng là database tại chỗ.

### Changed

- **`/api/knowledge/context` chỉ đòi khoá truy cập ở vai trò `server`.** Ở `all`/`edge` console
  là công cụ tại chỗ và phần còn lại của `/api/*` cũng không có khoá; bắt khoá thì UI trình
  duyệt phải nhúng khoá vào trang, tệ hơn là không bắt. VPS vẫn 401 khi thiếu khoá.

### Notes

- Đo độ trễ để loại trừ nghi ngờ sai: cùng một prompt, đi thẳng `api.vilao.ai` 1.2-1.8s (prompt
  nhỏ) và 1.9s (prompt 13.5 KB), qua proxy VPS 2.1-2.5s. **Proxy chỉ thêm ~0.3s** — độ trễ tỉ lệ
  với kích thước prompt (4k token ≈ 2s ⇒ 17k token ≈ 9s), không phải do đường truyền.
- Ngân sách mỗi task nâng lên **100 request / 5000đ** (trước 40/2000), đặt trên cả máy dev và
  VPS vì VPS là nơi đo lường thật sự. `BUDGET_DAILY_COST_VND` vẫn 6000đ/ngày — cần nâng nếu
  muốn chạy nhiều task trong ngày.

## [0.4.8] — 2026-10-26

### Added

- **`CONSOLE_ROLE` — tách console thành hai vai trò.** `server` (VPS) chỉ đăng ký AI proxy
  `/llm/v1` và API RAG `/api/knowledge/context`; `edge` (máy cắm điện thoại) chỉ đăng ký endpoint
  điều khiển thiết bị; `all` (mặc định) giữ nguyên hành vi cũ.

  Đây là **ranh giới bảo mật**, không phải cờ tiện dụng. `POST /api/run` chạy `artemis run` và
  `POST /api/stop` giết tiến trình, cả hai **không có xác thực** — đưa lên Internet là lỗ hổng
  thực thi lệnh. Route phải **không tồn tại** ở vai trò `server`, chứ không chỉ bị từ chối:
  middleware sót một đường là mất hết. Khoá lại bằng 25 test trong
  `tests/unit/test_console_role_split.py`, kiểm theo bảng route thật (`app.openapi()["paths"]`).

  `/api/kb/*` cũng bị loại khỏi `server`: nhóm route đó sửa và xoá được workflow mà **không có
  khoá truy cập riêng**. Người vận hành quản trị KB từ máy dev qua SSH tunnel.

- **AI proxy và API RAG nay chạy trên VPS `sep21th03.com`** (`103.199.18.222`), sau nginx có TLS:
  `https://sep21th03.com/artemis/llm/v1` và `https://sep21th03.com/artemis/api/knowledge/context`.
  Chạy dưới systemd (`artemis-console.service`), user riêng `artemis` không shell, bind
  `127.0.0.1:8787` nên cổng không lộ ra Internet. Trang `/` ở vai trò `server` là một trang trạng
  thái (vai trò, endpoint, tình trạng KB), không phải giao diện điều khiển.

- **`scripts/kb_tunnel.sh`** (`start` / `stop` / `status` / `restart`) — SSH tunnel tới PostgreSQL
  trên VPS, để máy dev quản trị KB mà cổng 5432 vẫn đóng với Internet.

### Fixed

- **Cloudflare chặn User-Agent mặc định của Python.** `sep21th03.com` đi qua Cloudflare; cùng một
  request, `Python-urllib/3.x` trả **403 (error 1010)** còn `ARTEMIS/1.0` trả 200. ARTEMIS nay gửi
  `User-Agent: ARTEMIS/1.0`. Triệu chứng khi thiếu header rất khó đoán — log chỉ nói *"không chèn
  kiến thức"*, y hệt một lần chạy không có gì để chèn.
- **`/api/health` không còn mô tả hệ thống tập tin trên máy chủ.** Ở vai trò `server` nó bỏ
  `adb_path`, `artemis_dir` và `devices`: máy chủ dùng chung không nên cho người gọi qua Internet
  biết đường dẫn nội bộ của mình, và nó cũng không có thiết bị nào.

### Notes

- Cài đặt trên VPS gặp hai cái bẫy đã ghi lại trong README: `uv` đặt Python vào `/root` (mode 700)
  nên systemd chết với `status=203/EXEC` — phải `UV_PYTHON_INSTALL_DIR=/opt/uv-python`; và cài
  `postgresql-14-pgvector` từ PGDG kéo theo apt nâng cấp luôn gói lõi `postgresql-14` và restart
  PostgreSQL (VPS này còn chạy `novel_platform`, `english_speaking` — không bị đụng).
- Máy dev và máy tester nay cùng trỏ về VPS cho cả LLM lẫn kiến thức. Đổi lại sự tập trung đó,
  VPS chết thì cả hai mất kiến thức và mất LLM — trước đây máy dev tự lo được. Muốn quay lại:
  đổi `OPENAI_BASE_URL` về `http://127.0.0.1:8787/llm/v1` và bỏ `ARTEMIS_KB_API`.

## [0.4.7] — 2026-10-26

### Removed

- **Bỏ hẳn tính năng Gợi ý & Luật.** Toàn bộ tab, API (`/api/guide*`, `/api/tasks/{id}/learn`),
  `GuideStore`/`GuideEntry`, hai backend lưu trữ, và tầng embedding riêng của nó đã bị gỡ —
  đo được **2.714 → 1.750 dòng** trong `apps/test_console/server.py` và **1.907 → 1.647 dòng**
  trong `static/index.html`. Kiến thức của app giờ chỉ có **một** nguồn: workflow trong KB.

  Không mất kiến thức nào: mục "Modal chuyển nhà" (thứ duy nhất chưa có trong KB) đã được
  chuyển thành workflow `home.switch_house_modal` (`status=verified`, có alias, có bước và
  tiêu chí thành công) trước khi gỡ. Hai mục còn lại đã trùng với `auth.logout` và
  `auth.switch_account`.

  Lý do bỏ: hai hệ thống kiến thức song song với hai cách khớp khác nhau (từ khoá vs lai
  RRF) và hai nơi lưu khác nhau là nguồn của mọi nhầm lẫn — cùng một chức năng có thể được
  chèn hai lần, hoặc bằng hai nội dung khác nhau.

### Changed

- **Luồng chạy nay là một chiều, rõ ràng:**

  ```
  Tester nhập prompt gốc
     ↓  POST /api/knowledge/context   (prompt gốc)
  Console: gọi Google Embedding → tìm workflow gần nhất trong PostgreSQL
     ↓  ghép prompt gốc + hướng dẫn của workflow
  Prompt hoàn chỉnh trả về cho tester
     ↓
  Tester chạy agent (ADB) với prompt hoàn chỉnh đó
  ```

  Console **không sửa prompt của tester** — chỉ nối khối hướng dẫn vào cuối. Không khớp được
  workflow nào thì trả lại đúng prompt gốc.

- **`POST /api/knowledge/context` trả về đúng bốn trường**: `prompt` (prompt hoàn chỉnh),
  `found`, `kb_prompt`, `kb`. Bỏ `guide_prompt` / `guide_names` / `guide_guessed` / `matched`
  / `semantic`.

- **Console không còn ghép gì vào goal trước khi chạy.** `goal_sent` nay bằng `goal`, nên
  prompt tester nhập và prompt agent nhận là một (trước đây console ghép thêm gợi ý vị trí và
  nhắc khoá app). `Task.guide_ids` và dòng `# guide:` trong log task cũng bị bỏ.

- **`Gợi ý vị trí` không còn tồn tại, nên `--profile flash` không còn nhận kiến thức nào.**
  Flash chạy `FlashRunner` (không có Planner), và KB chỉ phục vụ Planner. Trước đây flash vẫn
  dùng được Gợi ý & Luật; nay muốn có kiến thức phải chạy `--profile pro`.

### Added

- Workflow `home.switch_house_modal` — luật về modal "Chuyển nhà" hiện sau khi vào Trang chủ,
  chuyển từ Gợi ý & Luật sang KB để không mất kiến thức khi gỡ.
- `ARTEMIS_KB_API`, `ARTEMIS_KB_API_KEY`, `ARTEMIS_KB_API_TIMEOUT` — máy tester nhận prompt
  hoàn chỉnh qua HTTP, không cần PostgreSQL và không cần `GEMINI_API_KEY`.
- `KnowledgeContext.source` (`"database"` hoặc `"console"`) để dòng log RAG nói đúng khối kiến
  thức đến từ đâu.
- `scripts/kb_tunnel.sh` (`start` / `stop` / `status` / `restart`) — SSH tunnel tới PostgreSQL
  trên VPS, để cổng 5432 vẫn đóng với Internet mà máy dev vẫn dùng được như database tại chỗ.
- **Knowledge base nay nằm trên VPS `103.199.18.222`**: PostgreSQL 14.24 + pgvector 0.8.7,
  database `artemis_kb` (role `artemis`), nạp từ bản dump của máy dev — 3 workflow, 20 alias,
  10 bước, 6 tiêu chí, 4 điều kiện trước, 8 revision, 36 log, 3 vector 3072 chiều.
  Máy dev nối qua tunnel `127.0.0.1:5433`; máy tester **không cần database** (chỉ `ARTEMIS_KB_API`).
  Đã kiểm chứng bằng một workflow đánh dấu tạo trực tiếp trên VPS: console thấy ngay, xoá thì mất.
  Cài pgvector kéo theo việc apt nâng cấp gói lõi `postgresql-14` sang bản PGDG (cùng 14.24).

### Fixed

- **Bộ test không còn chạm vào knowledge base thật.** `tests/unit/conftest.py` nay vô hiệu hoá
  `get_service` cho mọi unit test: trước đây một test gọi `/api/knowledge/context` sẽ mở
  database thật và **gọi Google Embedding thật** (`.env` có sẵn `ARTEMIS_KB_DSN` và
  `GEMINI_API_KEY`).
- **Một test từng ghi `guide_vectors.json` vào thư mục `data/` thật** (128 B, id rác) — không
  còn khả năng đó vì tính năng đã bị gỡ.

### Notes

- Bảng `guide_entry` / `guide_embedding` không còn được tạo (đã bỏ khỏi `artemis/knowledge/schema.sql`).
  Bảng cũ còn lại trong database là vô hại và có thể `DROP` bất cứ lúc nào.
- Bản sao dữ liệu Gợi ý & Luật cũ vẫn nằm ở `apps/test_console/data/guide.json.imported` và
  `guide_vectors.json.imported`, phòng khi cần tra lại nội dung.

## [0.4.6] — 2026-10-26

### Fixed

- **Vector mồ côi tích tụ vô hạn trong cache của Gợi ý & Luật.** Xoá một gợi ý không xoá vector
  của nó trong `apps/test_console/data/guide_vectors.json`, nên file chỉ phình ra và mỗi lần
  ghi phải serialize lại toàn bộ. Đo trên máy này: **18 vector cho 3 mục đang sống, 743 KB**.
  Nay `_entry_vectors()` dọn mọi vector không còn mục tương ứng; dọn theo **toàn bộ** mục đang
  sống chứ không theo danh sách ứng viên truyền vào (mục `always` không bao giờ là ứng viên ngữ
  nghĩa, nên dọn theo danh sách ứng viên sẽ xoá nhầm vector của nó).

  Kết quả đo lại: **760.823 → 84.519 bytes, 18 → 2 vector**.

### Notes

- Vector của KB nằm trong PostgreSQL và **đã** được dọn đúng nhờ `ON DELETE CASCADE`
  (`kb_workflow_embedding.workflow_id` → `kb_workflow`). Chỉ cache dạng file của Gợi ý & Luật
  là thiếu bước dọn.
- Vector của prompt: Gợi ý & Luật có cache LRU 64 (`_PROMPT_VECTORS`), còn KB **không** cache
  — mỗi lần truy xuất là một lần gọi embedding mới (~0.5–0.8 s). Ghi lại đây để không quên.

## [0.4.5] — 2026-10-26

### Added

- **Kiến thức “Chuyển tài khoản”** — nút mũi tên trong ô nhập ở màn hình Đăng nhập mở bottom
  sheet danh sách tài khoản đã lưu; chọn tài khoản rồi bấm Đăng nhập (mật khẩu đã lưu sẵn).
  Dùng khi prompt chỉ nêu **tên/số tài khoản mà không có mật khẩu**.

  Thêm vào **cả hai** hệ thống vì chúng phục vụ hai profile khác nhau:

  - **KB workflow** `auth.switch_account` (cho `--profile pro`) — 4 bước: mở sheet → cuộn nếu
    cần → chọn đúng dòng tài khoản → bấm Đăng nhập; kèm 2 tiêu chí thành công để Checker chấm.
  - **Gợi ý vị trí** trong tab *Gợi ý & Luật* (cho `--profile flash`, vì flash **không có
    Planner** nên KB không được dùng).

  Đối chiếu `src/Components/Auth/LoginScreen.js` để mô tả đúng thay vì phỏng đoán:
  nút là `caret-down-outline` màu xanh ở cuối ô nhập; sheet có `scrollEnabled={true}`;
  chọn tài khoản chỉ **điền form** (`setPassword(item.password)`) chứ không tự submit.

  **Cảnh báo an toàn kèm theo:** mỗi dòng trong sheet có **icon thùng rác đỏ** — chạm vào đó
  là **XOÁ tài khoản** khỏi danh sách. Cả hai mô tả đều ghi rõ “TUYỆT ĐỐI KHÔNG chạm”.

### Fixed

- **Kênh ngữ nghĩa chiếm mất chỗ của mục khớp từ khoá** (lỗi có sẵn, lộ ra khi thêm mục mới).
  Truy vấn nearest-neighbour cho điểm dương cho **mọi** mục, và `match()` chèn bất kỳ mục nào
  có điểm > 0 — nên mỗi prompt luôn kéo thêm vài mục lạc đề và đẩy mục đúng xuống. Đo trên
  danh mục thật: mục lạc đề được **1–7** điểm, câu diễn đạt lại đúng nghĩa được **13–22**.

  Nay mục **chỉ** khớp ngữ nghĩa không tranh ngân sách chính: nó vào nhóm **“máy đoán”** có
  nhãn riêng và trần riêng (`GUIDE_MAX_RELATED_IN_PROMPT`), và phải vượt
  `GUIDE_SEMANTIC_STANDALONE_MIN` (12) mới được vào. Đo lại: `Test đăng xuất` chỉ còn chèn
  `Đăng xuất`, `mở app và đổi sang tài khoản 0999` chỉ còn chèn `Chuyển tài khoản`.
- **`/api/guide/preview` liệt kê một mục ở CẢ hai nhóm.** Mục chỉ khớp ngữ nghĩa nằm trong
  `names` (mà UI hiển thị là “khớp gợi ý vị trí”) **đồng thời** trong `guessed` (“máy đoán”).
  Nay `names` chỉ gồm mục khớp từ khoá / luật, `guessed` chỉ gồm mục đoán — hai tập rời nhau.
- **Từ khoá của mục “Đăng xuất”** thiếu `thoát tài khoản`, nên prompt *“thoát tài khoản đang
  dùng”* bị xếp vào nhóm máy đoán. Đã bổ sung `thoát tài khoản, thoát đăng nhập`.

### Tests

- `tests/unit/test_console_guide_budget.py` (7 test) — mục chỉ khớp ngữ nghĩa bị gắn nhãn
  “máy đoán”, không chiếm ngân sách chính, bị loại khi điểm yếu, và preview không được liệt
  một mục ở cả hai nhóm.

## [0.4.4] — 2026-10-26

### Added

- **Cảnh báo khi nhiều phần tử trùng nhãn** (`_inject_duplicate_text_warnings`,
  `artemis/utils/visualization.py`). Đây là nguyên nhân trực tiếp của ca `task_2610_1410`:
  màn hình có **hai** phần tử cùng tên **“Đăng xuất”** — hàng trong menu và nút trong hộp
  thoại nó mở ra — nên khi agent chọn theo chỉ số, nhãn không giúp phân biệt được gì.

  Nay danh sách phần tử mà agent đọc có thêm:

  ```
  [7] Text: 'Đăng xuất' | Bounds: [0,1093][692,1198] (WARNING: [10] carry the same text
      'Đăng xuất' — choose by position and context, not by text alone)
  ```

  Cùng kiểu cảnh báo mềm, nối thêm vào dòng mô tả như cảnh báo che khuất đã có
  (`_inject_mutual_occlusion_warnings`), nên danh sách vẫn là mô tả trung thực của màn hình.

### Fixed

- **Cảnh báo trùng nhãn không được bắn cho nhãn vô hình.** Đo trên màn hình thật: 2 trong 4
  cảnh báo là về các phần tử có `text` chỉ gồm ký tự zero-width — hiển thị ra không có gì,
  không ai có thể chọn theo tên, cảnh báo là nhiễu. Nay bỏ qua nhãn không có ký tự nhìn thấy
  được, nhưng vẫn giữ nhãn ký hiệu thật (`+`, `✓`).

## [0.4.3] — 2026-10-26

### Fixed

- **Console báo “ĐẠT” cho một task không làm được gì.** Phát hiện từ một phiên thật
  (`task_2610_1410`, goal *“mở app và đổi sang tài khoản 0999”*): agent lặp lại chạm nút
  **“Đăng xuất”** trong modal 21 bước, đăng xuất **không xảy ra** (kiểm tra lại thấy app
  vẫn còn đăng nhập), rồi agent gọi `report_task_status(status="completed")` — và console
  hiện **ĐẠT**.

  Nguyên nhân: `_task_summary()` kết luận `pass` **chỉ từ** `task.status == "success"`,
  tức tin ngay lời tự khai của agent, và **bỏ qua hoàn toàn** kết quả kiểm chứng
  (`RunOutcome.tests`). Nay verdict dựa trên **bằng chứng**:

  | Bằng chứng trong trace | Verdict |
  |---|---|
  | `tests.failed > 0` | **KHÔNG ĐẠT** — có tiêu chí đã kiểm chứng và thất bại |
  | `tests.passed > 0`, không failed/inconclusive | **ĐẠT** — Checker đã xác minh |
  | `tests.inconclusive > 0` | **CHƯA XÁC MINH** (thường do hết ngân sách LLM) |
  | Không có dòng `Run outcome:` (profile flash) | **CHƯA XÁC MINH** — “agent tự báo hoàn thành nhưng KHÔNG có tiêu chí nào được kiểm chứng” |
  | `passed == 0`, không tiêu chí nào | **CHƯA XÁC MINH** — không có gì để chấm |

  Thêm verdict `unverified` (màu hổ phách, icon ⚠️) cho UI, kèm 2 dòng giải thích *vì sao*
  chưa xác minh và *cách* có kết luận thật (chạy profile pro).

### Added

- **Cảnh báo trước khi chạy khi kiến thức sẽ vô tác dụng.** Bấm **Chạy test** mà goal khớp
  một workflow của app đang test, đồng thời profile là **Flash**, thì console hỏi lại:
  Flash không có Planner nên workflow sẽ không được dùng, và không có Checker nên kết quả
  chỉ là “chưa xác minh” — OK để chuyển sang Pro, Cancel để vẫn chạy Flash. Trước đây
  tester chỉ thấy dòng “đã chèn kiến thức” trong log và tưởng nó có tác dụng.

### Notes

- Đây là **hành vi có sẵn của ARTEMIS**, không phải do Workflow RAG gây ra: Flash luôn
  chấp nhận lời tự báo của agent, và console luôn tin nó. Nhưng nó chỉ lộ ra rõ khi có RAG,
  vì giờ ta kỳ vọng agent đi theo một quy trình đã biết.
- **Mọi task Flash cũ nay hiện “CHƯA XÁC MINH”** — đó là trạng thái thật của chúng: không
  tiêu chí nào từng được chấm. Muốn có verdict thật thì chạy `--profile pro`.
- Về phía app Hunonic (không thuộc ARTEMIS): phép chạm của agent **đúng** — toạ độ
  `[722, 562]` nằm trong bounds `[532, 534, 912, 592]` của nút modal (không gian
  normalized 0-1000). Sau cú chạm app chuyển sang màn hình đen rồi quay lại vẫn đăng nhập.
  Nghi vấn liên quan `adb reverse tcp:8081` rớt khi USB re-enumerate làm app debug không
  tải lại được JS bundle; cần một lần tái hiện thủ công để khẳng định.

## [0.4.2] — 2026-10-26

### Added

- **Workflow RAG — Phase 3: tab “Knowledge” trong Mobile Test Console**
  (`apps/test_console/static/index.html`) — quản lý knowledge base ngay trong UI tester
  đang dùng, không tạo app quản trị riêng.

  - **Danh sách app + workflow theo app**, có ô tìm kiếm khớp id / tên / mô tả / alias / tag.
  - **Trình soạn workflow**: id, tên, mô tả, alias, tag, trạng thái (draft / verified /
    deprecated), phiên bản app, điều kiện trước, **các bước**, tiêu chí thành công, ghi chú
    thay đổi (lưu vào lịch sử sửa).
  - **Trình soạn bước** theo hành động: chọn `tap` / `scroll` / `input_text` / … thì chỉ
    hiện đúng các ô có nghĩa (hướng, cuộn tới khi thấy, giá trị nhập, chờ tối đa), cộng cờ
    “bước này có thể không xuất hiện”. Nhãn nói thẳng: mô tả **ý định**, không phải toạ độ.
  - **Xoá mềm bằng trạng thái**: hộp thoại xoá nhắc rằng đổi sang “ngừng dùng” sẽ giữ được
    lịch sử sửa — vì `deprecated` không được truy xuất nữa.
  - **🔎 Kiểm tra truy xuất**: chạy đúng đoạn truy xuất của một lần test, hiện `found`,
    từng hit kèm **kênh khớp** và cờ **xác nhận / máy đoán**, timings từng kênh, và **in ra
    chính xác khối văn bản** sẽ được chèn vào Planner.
  - **📊 Kiến thức đã dùng**: đọc `kb_retrieval_log` (20 lần gần nhất, hoặc lọc theo run id),
    mỗi dòng có query, workflow được chọn, và **kết quả sử dụng** (`task_status` + đạt/hỏng/chưa rõ).
  - **Trạng thái embedding**: số workflow, model, cảnh báo khi **kênh vector tắt** (thiếu key),
    và nút tạo lại embedding.
  - KB tắt hoặc hỏng thì tab nói rõ lý do và khẳng định ARTEMIS vẫn chạy bình thường.

### Fixed

- **`api()` hiển thị lỗi validate thành `[object Object]`.** Lỗi 422 của pydantic trả
  `detail` là một mảng object; ném thẳng vào `Error` khiến tester không biết sai ở đâu.
  Nay render thành `steps.0.action: Value error, action không hợp lệ: 'teleport'…`.

### Tests

- `tests/unit/test_console_kb_ui.py` (17 test) — kiểm tra **tĩnh** trên `index.html`:
  mọi `$('id')` mà script dùng đều tồn tại, không có id trùng, mọi đường dẫn `/api/kb/…`
  mà script gọi đều có route đã đăng ký, và toàn bộ script **parse được bằng `node --check`**
  (tìm cả node trong `~/.nvm` vì máy này không có node trên PATH).
- `tests/integration/test_knowledge_pipeline.py` +3 test — chạy **đúng payload mà UI gửi**
  qua HTTP với PostgreSQL thật: tạo → sửa → kiểm tra truy xuất → xem log → xoá; thông báo
  422 đọc được; và hành vi khi KB hỏng (503 `degraded`).

## [0.4.1] — 2026-10-26

### Added

- **Workflow RAG — Phase 2: nối vào Planner** (`artemis/knowledge/integration.py`)
  `KnowledgeService.context_for()` được gọi trong `run_automation` **trước khi** agent
  khởi động, và kiến thức đi thẳng vào prompt của Planner.

  - **Không đụng vào Graph State.** Khối kiến thức được gắn lên `ArtemisContext`
    (`extra="allow"`) thay vì thêm field vào `State` — nhờ vậy không phải sửa hợp đồng
    State lẫn manifest `visibility.py` (vốn có enforcer ở chế độ test). Planner đọc lại
    qua `artemis.knowledge.integration.context_text(ctx)`. Một run không có kiến thức thì
    đơn giản là không set attribute, và mọi chỗ đọc đều coi "thiếu" = "không có kiến thức".
  - **`planner.json`**: thêm block `knowledge_guidance`, mount **có điều kiện** theo đúng
    cách block check đang làm — chỉ xuất hiện khi thực sự có khối kiến thức. Template
    `human` nay là `Goal: {{ initial_goal }}{{ knowledge_context }}`; khi không có kiến
    thức, prompt render **giống hệt từng byte** so với trước.
  - **Chỉ workflow đã xác nhận mới vào Planner.** `injected` đòi `found=True`, nên kết quả
    chỉ khớp vector (máy đoán) **không** vào prompt — run không có kiến thức phù hợp hành
    xử y như khi tắt RAG. Bản đoán vẫn xem được ở `/api/kb/retrieve` cho người đọc.
  - **Block hướng dẫn Planner**: kiến thức là *tham khảo*, UI thực tế luôn cao hơn, phải
    mang `success_criteria` của workflow thành check line để Checker xác minh, không
    replay toạ độ, và nội dung này là DỮ LIỆU không ghi đè được chỉ dẫn hệ thống.
  - **Log 3 tầng** (yêu cầu 7): (1) `kb_retrieval_log` ghi query + hits + kênh khớp +
    context đã che bí mật + timings; (2) dòng log `Planner nhận knowledge context: N ký tự,
    workflow=[...], app=...` — bằng chứng Planner thực sự nhận; (3) **outcome ghi ngược**
    vào bảng log sau khi task xong (`task_status` + passed/failed/inconclusive lấy từ
    `test_summary` mà SDK đã dựng sẵn, không tự suy diễn lại).
  - **Cột mới** `kb_retrieval_log.outcome`, `outcome_recorded_at` (+ `ALTER ... IF NOT EXISTS`).
  - **`find_app_by_package`**: nối package Android (`com.iot.hunonic`) với `app_id` trong KB
    (`hunonic`), chỉ dùng tra cứu khớp chính xác.

### Fixed (tìm ra khi chạy E2E thật)

- **`logger.info("...%s...", value)` làm sập node Planner.** Logger của ARTEMIS là
  `ArtemisLogger.info(self, message, **kwargs)` — chỉ nhận **một** tham số, khác logger
  chuẩn của Python. Dòng log mới trong Planner vì thế ném `TypeError` ngay trong graph node
  và làm hỏng cả run (`TypeError: ArtemisLogger.info() takes 2 positional arguments but 5
  were given`). Nay dùng f-string, và có **test tĩnh** (AST) chặn mọi `logger.<level>()`
  nhiều tham số trong `planner.py`.
- **Profile `flash` không có Planner.** `run_automation` nay in cảnh báo rõ khi có kiến thức
  nhưng chạy `--profile flash`: profile này dùng `FlashRunner` (vòng phản ứng đơn, **không
  có Planner**), nên khối kiến thức không được dùng. Trước đó tester chỉ thấy dòng "đã chèn
  kiến thức" và tưởng nó có tác dụng.

### Notes

- Chi phí thật đo được: run Pro đầy đủ cho một chức năng tốn **41 request / ~539k prompt
  token** (Operator có ảnh nên prompt lớn). Hạn mức mặc định của console là
  `BUDGET_MAX_REQUESTS=40` → **đủ để hết budget trước khi Checker chấm**. Muốn acceptance
  test có phán quyết thật thì nâng `BUDGET_MAX_REQUESTS` (và `BUDGET_MAX_TASK_SECONDS`).
- Hết budget thì hệ thống báo `inconclusive` chứ **không** báo Pass sai — đúng yêu cầu
  "không báo Pass khi chưa có bằng chứng xác minh".

## [0.4.0] — 2026-10-26

### Added

- **Knowledge base (Workflow RAG) — Phase 1: `artemis/knowledge/`**
  Agent được cho biết trước đường dẫn thao tác của từng chức năng thay vì tự mò.
  Nhập `Test đăng xuất` → truy xuất workflow `auth.logout` → các bước vào Planner.

  - **PostgreSQL + pgvector** (`schema.sql`, 11 bảng): nhiều app, nhiều phiên bản, aliases,
    preconditions, steps, success criteria, tags/metadata, draft/verified/deprecated,
    lịch sử sửa (revision snapshot), và `kb_retrieval_log` cho view "AI đã dùng kiến thức nào".
    Schema idempotent, tự tạo lúc khởi động; `ALTER ... IF NOT EXISTS` cho nâng cấp.
  - **Hybrid retrieval + RRF (k=60)**: ba kênh `exact` (id/alias) · `keyword` (FTS trên bản
    đã bỏ dấu) · `vector` (cosine). Hợp nhất bằng Reciprocal Rank Fusion nên chỉ cần **hạng**,
    không phải so các thang điểm vốn không so được với nhau (`ts_rank` vô biên, cosine ∈ [-1,1]).
  - **Embedding tách rời, có cache**: `GeminiEmbedder` batch + `task_type`; vector lưu kèm
    `content_hash` nên sửa một workflow chỉ embed lại **đúng workflow đó**. Đổi model là
    re-index, không mất dữ liệu (một dòng cho mỗi `(workflow, model)`).
  - **Chuẩn hoá tiếng Việt** (`text.py`): gộp dấu để `dang xuat` tìm được `đăng xuất`;
    stopword so khớp **trên chữ còn dấu** vì `đăng`/`đang` cùng thành `dang` (có test hồi quy).
  - **CRUD API** `artemis/knowledge/api.py`, đã mount vào Test Console tại `/api/kb/*`.
    Lỗi validate → **422** kèm thông báo đọc được; KB hỏng → **503** `degraded: true`, không bao
    giờ là 500. KB không cấu hình (`ARTEMIS_KB_DSN` trống) → ARTEMIS chạy y như trước.
  - **Test**: 85 unit + 23 integration (PostgreSQL thật) — gồm cả test **ngăn lấy nhầm kiến thức
    giữa các app và giữa các phiên bản**, test fallback khi KB/embedding hỏng, và acceptance test
    `Test đăng xuất`.

### Fixed (phát hiện trong lúc làm, đã sửa)

- **Kênh keyword từng khớp dựa trên một từ chung chung.** `chụp ảnh màn hình` được coi là khớp
  workflow đăng xuất vì cả hai có `màn hình`. Nay một hit chỉ được tính là **bằng chứng** khi
  phủ ≥ `ARTEMIS_KB_KEYWORD_MIN_COVERAGE` (0.6) số **token nội dung** (đã bỏ hư từ) của câu hỏi.
  Đo thực tế: yêu cầu thật đạt 1.0, ca dương tính giả đạt 0.5.
- **Không dùng ngưỡng cosine cố định để kết luận "có kiến thức".** Đo trên `gemini-embedding-2`
  với màn hình thật: yêu cầu lạc đề đạt 0.55–0.65 còn câu diễn đạt lại đúng nghĩa đạt 0.59–0.73 —
  hai phân bố chồng lấn, không cutoff nào tách được. `found` do kênh từ vựng quyết định; mục chỉ
  khớp vector được hiển thị dưới nhãn **"máy đoán"** và **không in các bước**.
- **`exc.errors()` của pydantic chứa object `ValueError`** không serialize được JSON → lỗi
  validate biến thành **500** thay vì 422. Nay lọc bỏ `ctx`/`input` trước khi trả về.
- **Workflow vượt trần ký tự bị bỏ hẳn** khiến khối văn bản báo "KHÔNG có workflow nào" — trong
  khi thực tế đã tìm thấy. Nay in phần vừa chỗ, và nếu không đủ chỗ cho cả tên thì nêu tên trong
  một dòng, tuyệt đối không nói là không tìm thấy.
- **`WorkflowRequest.id` bắt buộc** dù id đã nằm trong URL → client gửi thiếu là 422 trước cả
  handler. Nay là tuỳ chọn.
- **`chosen_workflow_id` ghi cả mục "máy đoán"** → view "AI đã dùng kiến thức nào" ghi nhận một
  workflow chưa từng được dùng. Nay chỉ ghi khi `trusted`.
- **`revision_no` lần lưu đầu trả về 0** (INSERT dùng default thay vì nhánh UPDATE) → nay bắt đầu từ 1.
- **Alias mất thứ tự tác giả** khi đọc lại (đang `ORDER BY alias`) → thêm cột `ordinal`.
- **Embedding batch trả 1 vector cho nhiều text**: `contents=[str, str]` là *một* content nhiều
  part. Phải truyền `list[Content]`.

### Notes

- `gemini-embedding-2` **bỏ qua** `task_type` (vector query/document của cùng text giống hệt,
  cosine 1.0000). Tham số vẫn truyền vì model khác dùng thật.
- `kb_workflow_embedding.embedding` là `vector` **không định độ rộng** để nhiều model cùng tồn tại;
  đổi lại không tạo được index HNSW (quét toàn bộ, vài ms cho vài nghìn workflow).
- Chi phí mỗi lần truy xuất ~0.5–0.8 s, gần như toàn bộ là gọi embedding; hai kênh SQL chỉ ~10 ms.
- **Phase 2–4 chưa làm**: Planner chưa gọi `context_for()`; UI quản lý workflow chưa có
  (API đã đủ); học workflow từ phiên chạy chưa làm.

## [0.3.2] — 2026-10-23

### Added

- **Gợi ý & Luật: ngân sách chèn tách riêng + vòng "train"** (sửa lỗi và thêm 3 vòng phản hồi):
  - **Bug**: luật `Luôn chèn` và mục khớp từ khoá dùng **chung một trần** nên chỉ cần 4 luật là
    mọi gợi ý vị trí bị đẩy ra ngoài — prompt nói về "đăng xuất" mà không còn được chỉ đường tới
    nút Đăng xuất. Nay hai nhóm có trần riêng (`GUIDE_MAX_ALWAYS_IN_PROMPT` = 3, và
    `GUIDE_MAX_ENTRIES_IN_PROMPT` nâng 3 → 5, khối 900 → 1.600 ký tự ≈ 600 token ≈ 0,3đ/lượt).
  - **🎓 Học thành gợi ý**: nút trên card kết luận dựng sẵn một gợi ý từ chính các bước vừa chạy
    (từ khoá lấy từ **nhãn phần tử agent đã chạm**; đường dẫn là chuỗi bước). Không tự lưu — tester
    xem lại rồi bấm Thêm. App dùng nút **chỉ có icon** thì nói thẳng là không đọc được nhãn thay
    vì ghi ra "Chạm → Chạm → Chạm".
  - **Thống kê hiệu quả**: mỗi mục hiện `Chèn N lần · ĐẠT x/y task` (`GET /api/guide`) để biết mục
    nào thực sự giúp; chèn nhiều mà vẫn KHÔNG ĐẠT nghĩa là phải viết lại **nội dung**.
  - **Lý do từng bước**: thẻ bước có thêm dòng 🧠 từ `operator_raw_thinking` của daemon. Với app
    dùng nút icon (không có nhãn chữ) đây là thứ duy nhất giải thích agent đang cố làm gì.
- **Gợi ý & Luật: hai loại mục, không chỉ "chỉ đường"**: tab *Gợi ý vị trí* nay có thêm loại
  📜 **Luật của app** — dạy agent những thứ nó **không thể tự suy ra**: một modal hiện ra sau khi
  đăng nhập nghĩa là gì, cảnh báo nào được phép bỏ qua, trạng thái nào là bình thường. Luật được
  chèn dưới header riêng `[LUẬT CỦA APP — hãy hiểu và tuân theo]` và **luôn đứng trước** gợi ý vị
  trí (luật nói *cái này là gì*, gợi ý chỉ nói *nút ở đâu*). Luật chung của app bật được
  **Luôn chèn vào mọi prompt** (không cần từ khoá) và luôn xếp trên các mục theo từ khoá. Tab đổi
  tên thành **Gợi ý & Luật**; API thêm `kind` (`position`/`rule`) và `always`.
- **Nhắc agent bám đúng app (bịt lỗ hổng của `--locked-app`)**: `--locked-app` của ARTEMIS chỉ
  đảm bảo app ở foreground **lúc khởi động** — nó không ràng buộc hành động sau đó và agent
  cũng không được thông báo. Đo thực tế: với prompt không nêu tên app, agent tự
  `manage_app(stop, "Tuya Smart")`, bấm `HOME`, mở `APP_SWITCH`, vuốt 6 lần và chạm cả
  "Zing MP3" — 20 lượt rồi bị cắt. Console nay chèn khối `[APP ĐANG TEST]` vào prompt (kèm
  package, cấm mở/tắt app khác, cấm HOME/app switcher, yêu cầu báo FAILED thay vì thử lung
  tung). Cùng prompt đó sau khi thêm: **3 hành động đúng** — stop rồi launch `com.iot.hunonic`.
  Tắt bằng `CONSOLE_APP_LOCK_HINT=0`.
- **Sửa loader để `${env:...}` thực sự có tác dụng (bug tiểu nhân tự gây rồi tự sửa)**: hai loader
  thật của runtime là `parse_llm_config` (`artemis/config/llm.py`) và `load_agent_config`
  (`artemis/config/agent.py`) đọc file bằng `load_jsonc` **không qua interpolate**, nên placeholder
  lọt thẳng vào các field `Literal` và làm task **không chạy được**:
  `Task execution failed: 5 validation errors for LLMConfig`. Nay cả hai loader gọi
  `interpolate_config_value` sau khi đọc file, nên `.env` điều khiển được cả model **và** provider.
- **Bật model Gemini ER cho `object_detector`** (đã kiểm chứng trên máy thật): key Gemini free tier
  gọi được `gemini-robotics-er-2-preview` và trả toạ độ **chính xác** trên ảnh chụp màn hình
  (`[{"box_2d": [224, 353, 356, 649], "label": "Cửa cuốn"}]` khớp đúng ô trên màn hình). Node này
  trước đây chạy `flash-lite` dù chính comment trong config ghi nó **phải** dùng model ER, nếu không
  sẽ bấm sai toạ độ. Fallback tự về Vilao nên không phụ thuộc free tier. Kèm `_PROVIDER` cho
  `planner`/`operator`/`explorer`/`checker` để đổi sang `google` bằng 1 dòng `.env`.
- **Model theo từng chức năng, đọc từ `.env`**: `config/artemis.jsonc` nay lấy tên model của
  `default`, `planner`, `operator`, `explorer`, `object_detector`, `hopper`, `checker`,
  `step_summarizer` và `chunking` qua cú pháp `${env:VAR:-mặc-định}` của ARTEMIS. Quy ước tên
  `<MODEL>_<CHỨC NĂNG>` (ví dụ `GEMINI35FLASH_OPERATOR`). **Không khai biến nào thì dùng đúng giá
  trị cũ** (`ts/gemini-3.5-flash-lite`) nên nâng cấp không đổi hành vi lẫn chi phí; đổi model cho
  một chức năng = sửa 1 dòng `.env` + restart daemon. Chỉ 2–3 node thật sự cần model mạnh (nhìn
  màn hình + quyết định), phần còn lại chỉ đọc chữ nên giữ model rẻ.
- **Cảnh báo thiếu đường Metro cho app React Native bản debug**: bản debug không nhúng JS
  bundle, nên khi Metro tắt hoặc `adb reverse tcp:8081` mất (rút/cắm cáp, restart adb server)
  app **kẹt ở màn hình splash vĩnh viễn**. Agent không biết, cứ `wait_for_delay` rồi chạm lung
  tung — đo được 10 lượt chờ liên tiếp trên app Hunonic. Console nay kiểm tra `adb reverse`
  trước khi chạy và cảnh báo ngay trên UI + trong log task. Đổi cổng bằng
  `CONSOLE_RN_METRO_PORT`, tắt bằng `CONSOLE_RN_METRO_PORT=0`.
- **Diễn biến của agent trong tab "Các bước"**: task có thể kết thúc mà không có thao tác
  chạm/vuốt nào (agent chỉ quan sát rồi kết luận), khiến tab trống trơn dù đã tốn thời gian
  và tiền. Console nay đọc trace và hiện danh sách *Diễn biến của agent* (lượt, thao tác,
  lỗi, cảnh báo, thông tin) — tự mở sẵn khi không có bước nào.
- **Preset "Tắt app rồi mở lại"** và nhắc viết prompt rõ điều kiện thành công, để agent
  không đi lạc khi prompt mơ hồ kiểu *"gạt app đi và vào lại rồi ấn đăng nhập"*.
- **Hướng dẫn cài trên máy khác** (`apps/test_console/README.md`): 2 kịch bản (máy chạy
  console / máy chỉ cắm điện thoại), checklist kiểm tra, bảng xử lý sự cố khi cài.
- **Khoá truy cập API cho Test Console**: `/llm/v1` nay yêu cầu `Authorization: Bearer <khoá>`
  (hoặc `x-api-key`), mặc định `CONSOLE_ACCESS_KEY=21092003`. Nhờ vậy console có thể mở ra VPS/LAN
  mà không lộ ví LLM: client (máy tester) chỉ giữ khoá truy cập, key provider thật nằm trên máy chạy
  console và được proxy thay vào trước khi gọi upstream. Chế độ `LOCAL_LLM=1` (Ollama) miễn khoá;
  key trong `.env` của máy chạy console luôn được chấp nhận nên cấu hình cũ không đổi.
- **Gợi ý vị trí (`data/guide.json`)**: tab *Gợi ý vị trí* cho tester khai báo đường dẫn tới từng
  nút/chức năng kèm từ khoá nhận biết. Khi prompt khớp từ khoá, console chèn khối `[GỢI Ý VỊ TRÍ]`
  vào prompt trước khi gọi `artemis run`, nên agent biết nút ở đâu (áp dụng cho cả Flash và Pro,
  không cần sửa runtime ARTEMIS). Kèm API `GET/POST /api/guide`, `DELETE /api/guide/{id}`,
  `POST /api/guide/preview`, đếm số lần dùng, bật/tắt từng gợi ý, và xem trước gợi ý nào sẽ được
  chèn ngay khi đang gõ prompt.

### Fixed

- **Chạy nhầm app vì ô Package**: console chỉ lưu prompt vào `localStorage`, không lưu
  ô **Package** — nên F5/tắt tab là ô này lặng lẽ quay về `com.example.app`. ARTEMIS
  khi đó thử mở app không tồn tại **3 lần × 15s** rồi bỏ cuộc, tester chỉ thấy *"app
  không chạy"* mà không biết vì sao. Nay có 3 lớp: (1) Package + chế độ Flash/Pro được
  lưu lại; (2) ô Package gợi ý từ `GET /api/packages` (app đã cài trên máy) và cảnh báo
  đỏ ngay khi tên không tồn tại; (3) `POST /api/run` **từ chối** (`400`) nếu package
  chưa cài, kèm gợi ý tên gần đúng (`com.iot.hunonicc` → `com.iot.hunonic`) và bỏ qua
  kiểm tra khi không gọi được `adb` (không chặn oan).
- **Task "treo" ở bước 0 khi agent tự tắt/mở lại app**: prompt kiểu *"tắt app đi và
  vào lại rồi đăng nhập"* khiến agent gọi `manage_app(action="launch")`, khởi động
  sub-agent Hopper. Model trả JSON bọc trong ```` ```json ````, `with_structured_output`
  parse lỗi, và vì lỗi này bị xếp vào nhóm retry được nên ARTEMIS retry rồi pause chờ
  resume — mặc định **900s (15 phút)**, nên tester chỉ thấy "Đang chạy" và app không
  bao giờ được mở. Nay `RobustChatModelWrapper.with_structured_output` bọc chain trong
  `_TolerantStructuredRunnable`: cứu câu trả lời qua `parse_structured` (vốn đã hiểu
  fence và JSON noise) từ **hai** nguồn — kết quả `include_raw` và, quan trọng hơn,
  `errors()[*]["input"]` của chính `ValidationError` mà nhánh `method="json_schema"`
  (mặc định của langchain-openai ≥ 0.3) ném ra kể cả khi `include_raw=True`. Cứu từ
  lỗi nên **không tốn thêm lượt gọi LLM nào**. Áp dụng cho mọi structured output
  (Hopper, Checker, Planner…), không chỉ Hopper. Kèm `LLM_PAUSE_TIMEOUT_SECONDS=120`
  trong `.env` để nếu còn pause thì bỏ cuộc sau 2 phút thay vì 15 phút.
- **UI hiển thị session/bước của lần chạy TRƯỚC** (`_resolve_session`): hàm này đoán session
  theo nội dung prompt trong ±180s rồi **cache bản đoán** vào `task.session_id`; khi stdout in
  ra session thật thì `_pump` bỏ qua vì `task.session_id` đã có giá trị. Hệ quả: chạy lại cùng
  một prompt là UI hiển thị bước của lần trước — hoặc **0 bước** nếu session cũ không có thao
  tác chạm/vuốt (đo được: task `3c6c4e7f` hiện 0 bước, sau khi sửa hiện đúng **22 bước**). Nay:
  session đọc từ stdout là **nguồn chính xác** và luôn ghi đè bản đoán; bản đoán chỉ nhận session
  **bắt đầu sau** khi task chạy và **chưa bị task khác chiếm**.
- `_resolve_session` so khớp `initial_goal` bằng prompt thực sự gửi đi (`goal_sent`) thay vì prompt
  gốc, để việc chèn gợi ý vị trí không làm mất liên kết session → steps → kết luận.

### Docs

- **Đo và ghi lại hạn mức thật của Gemini free tier** (Google đã bỏ bảng hạn mức khỏi tài liệu
  nên phải tự đo). Gửi request liên tiếp tới khi bị 429 rồi đọc chính body lỗi — 429 kèm
  `QuotaFailure` ghi rõ tên quota và giá trị:

  ```
  gemini-robotics-er-2-preview  → 429 ở request #8, quotaVal 5, retryDelay 58s
  gemini-3.5-flash              → 429 ở request #9, quotaVal 5, retryDelay 35s
  quotaId: GenerateRequestsPerMinutePerProjectPerModel-FreeTier
  ```

  Kết luận: **5 RPM** và **20 RPD** cho nhóm Flash (kể cả model ER); **15 RPM / 500 RPD** cho
  `gemini-3.5-flash-lite` và `gemini-3.1-flash-lite`. Hạn mức tính **theo project, không theo
  key**; RPD reset nửa đêm giờ Thái Bình Dương. Vì 20 request/ngày chỉ đủ 1–2 task ARTEMIS nên
  **vòng lặp chính phải ở lại Vilao**, chỉ `object_detector` dùng `google`.
- **Kiểm chứng lại định dạng trả về của `object_detector` bằng đúng prompt của ARTEMIS**: với
  `object_detector.json` (template + `instructions` đòi `point`), cả
  `gemini-robotics-er-2-preview` (`[{"point": [290, 500]}]`, 2.7s) và `gemini-3.5-flash-lite`
  (`[{"point": [304, 528]}]`, 2.8s) đều trả đúng `point` — không cần xử lý `box_2d`. Vẽ cả hai
  điểm lên ảnh `720×1600` thì cả hai đều nằm trong thẻ "Cửa cuốn". Ghi nhận
  `gemini-3.5-flash-lite` (Google trực tiếp, 500 RPD) là phương án thay thế khi cần chạy nhiều
  task/ngày — khác serving với `ts/gemini-3.5-flash-lite` qua Vilao vốn từng trả sai toạ độ.
- **Cảnh báo quyền riêng tư**: free tier dùng nội dung gửi lên để cải thiện sản phẩm của Google,
  nên ảnh chụp màn hình app khách hàng có thể bị dùng cho huấn luyện.

## [0.3.1] — 2026-10-21

### Added

- `artemis llm check [--json] [--timeout]`: gọi model một lần và in **nguyên văn body** khi lỗi
  (401/404/429/HTML) kèm gợi ý sửa, để đọc được các "JSON error" của provider/proxy.
- `POST /api/budget/reset` trên Test Console để gỡ trạng thái `429 budget_exceeded`.

### Fixed

- Test Console proxy khoá vĩnh viễn sau 480s với các lệnh CLI: mọi request CLI dùng chung một task
  `external` và timer không bao giờ reset → sau 8 phút mọi request trả
  `{"error":{"code":"budget_exceeded"}}`. Nay task `external` tự reset sau
  `BUDGET_EXTERNAL_IDLE_RESET_SECONDS` (mặc định 120s) không có request.


## [0.3.0] — 2026-10-21

### Added

- **Remote ADB server support** (DEV machine runs ARTEMIS, tester machine runs
  `adb -a -P 5037 nodaemon server` with the phone on USB):
  - `artemis/runtime/adb_status.py`: endpoint reachability (TCP) + `adb devices -l`
    inventory with `device` / `unauthorized` / `offline` states and one actionable
    hint per failure mode.
  - `artemis adb devices` / `artemis adb check` (with `--host`, `--port`, `--json`).
  - `AdbEndpoint.apply_to_environment` now also exports `ANDROID_ADB_SERVER_HOST/PORT`
    (the names adbutils/uiautomator2 actually read) and `apply_current_endpoint()`
    rebinds the module-level `adbutils.adb` client.
  - `apply_current_endpoint()` is applied at CLI, MCP server and Admin Console startup.
  - `artemis doctor` reports the endpoint, its reachability and a remote-specific fix list.
  - `artemis ui --host 0.0.0.0` prints the LAN URLs testers can open.

### Fixed

- Device enumeration (`device_pool`), live screen + device list (`device_stream_service`),
  replay (`replay_manager`), `run_adb_command` (`command_tool`), `dumpsys` display state
  (`utils/video.py`) and MCP device utils no longer run bare `adb`: every call goes through
  `adb -H <ADB_HOST> -P <ADB_PORT>`, so nothing silently falls back to `localhost`.
- `adb start-server` / `kill-server` only run for a local endpoint; with a remote server
  ARTEMIS just verifies connectivity instead of spawning a server on the DEV machine.

### Changed

- Accessibility Helper is skipped automatically with a remote ADB server (`adb forward`
  binds on the tester's loopback) and falls back to UIAutomator2; override with
  `ARTEMIS_ALLOW_REMOTE_HELPER=1` when the forward port is reachable (SSH tunnel).
- scrcpy video recording logs a warning under a remote endpoint: scrcpy needs
  `ADB` to point at a wrapper script (documented in README 17.4).

## [0.2.0] — 2026-10-21

### Added

- **Mobile Test Console** (web UI tiếng Việt cho tester) nằm ngay trong repo tại
  `apps/test_console/`: `server.py`, `static/index.html`, `run.sh`, `README.md`,
  `Modelfile.artemis-local`. Console **self-contained**: mặc định dùng chính
  `.venv` / `.env` / `config/artemis.jsonc` của repo (override bằng `ARTEMIS_DIR`),
  nên clone ở đâu chạy cũng được.
- `scripts/artemis.sh` — wrapper CLI tự thêm `adb` vào PATH, dùng `.venv/bin/artemis`
  hoặc fallback `uv run`.
- Mục 16 trong `README.md` hướng dẫn chạy console, đổi cổng/package, cảnh báo proxy LLM.

### Changed

- `apps/test_console/server.py`: thêm `artemis_launcher()` — dùng `.venv/bin/artemis`
  nếu có, fallback `uv run --project <repo> artemis`; `/api/status` báo `artemis_ok`
  theo launcher này. Package mặc định đọc từ `TEST_APP_PACKAGE` (mặc định `com.example.app`).
- `apps/test_console/run.sh`: `ARTEMIS_DIR` mặc định là repo root, fallback `uv run`
  khi chưa có `.venv`.
- Gốc repo `.gitignore` thêm `apps/test_console/data/` (log, history, budget là runtime).

## [0.1.0] — 2026-10-21

Bản đầu tiên được đóng gói từ workspace `Developer`.

### Fixed

- **Ctrl+C / Stop không dừng agent**: `artemis run` chỉ submit task vào Artemis Daemon rồi chờ,
  nên tín hiệu dừng chỉ giết tiến trình CLI và daemon vẫn tiếp tục điều khiển điện thoại.
  Cầu nối `SIGTERM → KeyboardInterrupt` nay được cài **trước** khối routing daemon
  (`artemis/interfaces/cli/commands/run.py`), để mọi tín hiệu dừng đi vào nhánh
  `except KeyboardInterrupt` → `stop_task_on_daemon(session_id)` → `SystemExit(130)`.

### Changed

- `config/artemis.jsonc`: model mặc định và hầu hết node chuyển sang `provider: openai`
  với gateway OpenAI-compatible (`model: ts/gemini-3.5-flash-lite`); cần đặt `OPENAI_BASE_URL` + `OPENAI_API_KEY`.
- `profiles.flash.max_turns` = 25, `explorer_mode` = `pro`.
- Tắt `flash.step_summarizer` và block `context` (chunk compression) vì phụ thuộc Gemini key native.
- `operator.thinking_level` = `low`, `include_thoughts` = `false` để giảm token mỗi bước.

### Docs

- `README.md`: hướng dẫn tiếng Việt đầy đủ (cài đặt → cấu hình → chạy → MCP → dừng task → troubleshooting).
- README gốc tiếng Anh đổi tên thành `README_EN.md`, bản tiếng Trung giữ nguyên `README_CN.md`,
  cập nhật liên kết chéo và URL clone của repo này.
