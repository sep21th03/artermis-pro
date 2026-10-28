# Changelog — ARTEMIS Pro

## [Unreleased]

### Changed

- **Proxy LLM của console đi thẳng Google, bỏ hẳn Vilao.** Đo trên máy thật 28/10/2026: lượt
  chạy 13:47 đứng ở "Lập kế hoạch" rồi bị console giết sau **120s không có log mới**
  (`⛔ Dừng task: treo 120s`). Nguyên nhân **không phải model** và cũng không phải Gemini: agent
  gọi proxy console (`127.0.0.1:8787/llm/v1/chat/completions`), proxy chuyển tiếp tới
  `ARTEMIS_UPSTREAM_BASE_URL=https://api.vilao.ai/v1`, và upstream này **từ chối kết nối**
  (`httpx.ConnectError: All connection attempts failed`, 18 lần `500 Internal Server Error`).
  SDK OpenAI retry trong im lặng (mỗi lần ~20s connect), console không thấy log mới nên giết task.

  - `.env`: `ARTEMIS_UPSTREAM_BASE_URL` → `https://generativelanguage.googleapis.com/v1beta/openai`
    (API tương thích OpenAI của Google). Đây cũng là đường đã đo **nhanh hơn** gateway từ 27/10:
    Google 0,9–1,6s so với gateway 4,3–56,9s (có lượt kèm ảnh >200s).
  - `apps/test_console/server.py`: khoá xác thực chọn **theo upstream** (`upstream_key()`): đi
    Google thì dùng `GEMINI_API_KEY`, gateway thì `OPENAI_API_KEY` — gửi sai khoá thì Google trả
    401 và người đọc log tưởng lỗi cấu hình của mình. Tên model của gateway (`ts/…`) được bỏ tiền
    tố khi đi Google (`normalize_upstream_model`) nên **phía agent không phải đổi gì**.
  - `apps/test_console/server.py`: **fail-fast** thay vì treo — `httpx.Timeout(120s, connect=5s)`,
    và `ConnectError`/`Timeout` trả **502/504 kèm tên upstream** thay vì 500 rồi để SDK retry mù.

  Kiểm chứng: gọi proxy đúng kiểu harness gửi (`model: ts/gemini-3.5-flash-lite`) ⇒ **200 trong
  1,2s**, response có `model=gemini-3.5-flash-lite` + `thought_signature` của Google. Lượt chạy
  thật qua console (task read-only): **xong trong ~18 giây**, 0 lần `500`. Upstream chết thật
  (`127.0.0.1:9`) ⇒ **502 trong 0,0s** kèm `upstream_unreachable` và tên host. 3 test mới.

- **Chuỗi xoay model trỏ vào model đã chết (404).** Kiểm bằng chính key này qua endpoint Google
  (28/10/2026): `gemini-2.5-flash` và `gemini-2.5-flash-lite` trả **404 "no longer available to
  new users"**, trong khi `gemini-3.5-flash-lite` (1,3s), `gemini-3.1-flash-lite` (3,6s),
  `gemini-3.5-flash` (1,9s) và `gemini-robotics-er-2-preview` (1,1s) đều chạy được.  Vì 404 bị
  `classify_failure` xếp vào `BAD_REQUEST` (**không** xoay được), một model chết nằm trong chuỗi
  là **chốt chặn giả**: đúng lúc model chính cạn quota, lượt xoay sẽ gặp 404 và dừng hẳn.

  - `.env`: bốn chuỗi `ARTEMIS_LLM_CHAIN_*` chỉ còn model đã kiểm (`gemini-3.1-flash-lite`,
    `gemini-3.5-flash`), kèm bảng kết quả kiểm để lần sau không phải đo lại.
  - `config/artemis.jsonc`: `default.fallback` và preset `gemini-flash` chuyển hết về Google
    (bỏ `ts/…` và `gemini-2.0-flash`) ⇒ **mọi chuỗi của cả 4 vai trò giờ kết thúc trong Google**,
    không còn mắt xích nào ra gateway bên thứ ba.

### Added

- **Action Guard cho Flash + kiểm soát thao tác lặp (P0), và trace đọc lại được (P1).**
  Trước đây **chỉ Pro** có safety net: `agents/validator/precondition_xml.py` kiểm phần tử đích
  còn trên màn hình live trước khi thực thi, còn Flash bắn thẳng. Nay Flash **gọi lại đúng hàm
  guard đó** (`validate_action_precondition_single`) trước khi bắn, với ba nguyên tắc để không
  chặn oan:

  - chỉ chặn khi guard **có bằng chứng** (phần tử đã đổi/mất);
  - **không** chặn khi không đọc được hierarchy live (``xml_bypassed``) — thiếu dữ liệu không
    phải bằng chứng cú chạm sai;
  - guard lỗi vì bất kỳ lý do gì ⇒ **vẫn bắn** và ghi cảnh báo (guard không bao giờ làm gãy lượt chạy).

  Kèm theo, **kiểm soát lặp** dựa trên vân tay UI + action + target: chỉ những action *kỳ vọng*
  màn hình đổi (``click``/``long_press``/``click_sequence``) mới bị đếm; ``input_text``, ``swipe``,
  ``wait``, ``assert``… không bao giờ bị coi là "lặp vô ích". Khi cùng một (action, target) đã
  không làm UI đổi tới hạn thì lần lặp kế tiếp **được trả về cho model kèm mô tả trung thực**
  ("UI không đổi — chưa phải bằng chứng action thất bại; hãy quan sát lại và đổi cách làm"),
  đúng mức đã ghi trong prompt KB ("không lặp lại y nguyên quá 2 lần").

  P1: kết quả mỗi action mang thêm ``guard`` (đã kiểm chưa, kết luận, lý do) và ``effect``
  (vân tay UI trước/sau, ``ui_changed``) — vào cả span trace lẫn metadata của tool result, nên
  đọc lại được vì sao một action bị chặn. Vân tay dùng **đúng** hàm
  ``AndroidAdbDriver._ui_fingerprint`` mà đường Pro đã dùng, không băm ở chỗ thứ hai.

  Phạm vi: ``ActionExecutor`` **chỉ** được Flash dùng (Pro đi qua ``action_execution.exec_action``),
  nên cơ chế Pro không đổi. 10 test mới; tắt guard + chống lặp ⇒ **3 test fail** đúng như mong đợi.
  Không thêm lượt gọi LLM nào ở đường thuận lợi — chỉ khi chặn (và khi đó thay cho một lượt chắc
  chắn phải chạy lại).

### Fixed

- **Xoay model khi cạn quota — "model này hết thì dùng model khác", ưu tiên provider Google.**
  Đo trên máy thật 28/10/2026: bốn vai trò (`planner`/`operator`/`explorer`/`checker`) cùng trỏ
  `google:gemini-3.5-flash-lite`; quota Free Tier của Google là **500 request/ngày/mỗi model**,
  cạn trong một buổi chạy — và vì mỗi vai trò chỉ có **một** model nên mọi lượt sau đều 429 cho
  tới khi hết ngày (`retryDelay` 14269s ≈ 4 giờ). ARTEMIS đứng hẳn: task báo "initializing" rồi
  thoát, không chạy được bước nào.

  - `artemis/llm/reliability.py`: `parse_retry_delay` (đọc `"retryDelay": "14269s"`, câu
    *"Please retry in 3h57m49s"*, header `retry-after`), `is_daily_quota`, và `quota_cooldown`
    — **quota ngày ≠ rate limit theo phút**: cái trước phải *đổi model*, cái sau chỉ cần chờ.
    `CircuitBreaker.record_failure(..., open_for=…)` mở mạch **ngay** bằng thời gian nhà cung cấp
    nói, không đợi đủ `threshold` lần (đếm đủ 3 lần là tiêu thêm 2 request vào đúng quota vừa cạn).
  - `artemis/services/llm.py`: chuỗi ứng viên cho mỗi vai trò — model trong config, rồi
    `ARTEMIS_LLM_CHAIN_<VAI TRÒ>` trong `.env`, rồi `fallback` của config (tương thích cách khai
    cũ). `_pick_healthy` bỏ qua model đang nghỉ và dùng model kế; hết ứng viên khoẻ mạnh thì chọn
    cái sớm hồi nhất **và ghi cảnh báo kèm thời gian hồi**, không chặn tác vụ (chặn cứng biến một
    rate limit 60 giây thành một tác vụ chết). Mục trong chuỗi không ghi `provider:` thì giữ
    provider của vai trò, nên `_PROVIDER=google` là **cả chuỗi chạy trên Google** — đúng "ưu tiên
    Google", không phải lặp lại ở từng mục; muốn một mục đi đường gateway thì ghi
    `openai:ts/gemini-3.5-flash-lite`.
  - Đường xoay **trong cùng một lượt gọi** dùng lại cơ chế `with_fallback` sẵn có: khi model chính
    vừa 429, `_resolve_endpoint(use_fallback=True)` trả ứng viên khoẻ kế tiếp, nên lượt đang chạy
    không hỏng.
  - `.env` + `.env.example`: khai chuỗi cho 4 vai trò, Google trước
    (`ARTEMIS_LLM_CHAIN_OPERATOR=gemini-2.5-flash,gemini-3.1-flash-lite,gemini-3.5-flash`…).
    `object_detector` **cố ý không** khai chuỗi: định vị toạ độ chỉ model ER làm được, đổi sang
    model thường là trả toạ độ sai — thà báo hết quota còn hơn bấm sai chỗ.

  Kiểm chứng: 16 test mới; tắt phần chọn ứng viên ⇒ **đúng 2 test xoay model fail** như mong đợi.
  Chạy thật với config + `.env` thật: chuỗi đọc đúng thứ tự, và khi
  `google:gemini-3.5-flash-lite` bị cho nghỉ 14269s thì lượt sau tự dùng
  `google:gemini-2.5-flash`; nghỉ tiếp 2.5-flash ⇒ `gemini-3.1-flash-lite` ⇒ `gemini-3.5-flash`
  ⇒ cuối cùng mới sang `openai:ts/gemini-3.5-flash-lite`.

### Fixed

- **Prompt bỏ qua việc đăng nhập dù tester yêu cầu đăng nhập bằng một tài khoản cụ thể.**
  Câu *"đăng nhập tài khoản 0999 rồi vào màn ngữ cảnh…"* sinh prompt có dòng
  `1. đăng nhập tài khoản 0999 — chỉ khi chưa đăng nhập`, và agent **bỏ qua việc đăng nhập** —
  vào thẳng màn Ngữ cảnh. Đo trên máy thật (SM-A066B): app đang đăng nhập **0972842548**, tức
  một tài khoản KHÁC `0999`. Nguyên nhân không nằm ở agent: điều kiện `"chỉ khi chưa đăng nhập"`
  là phép kiểm **nhị phân** (đang đăng nhập bằng tài khoản nào cũng tính là "đã đúng"), trong
  khi yêu cầu thật là **so giá trị**. Prompt cũng không nói cách quan sát *đang đăng nhập bằng
  tài khoản nào* — KB chưa có workflow nào đọc giá trị đó — và không nói *đạt* điều kiện trước
  bằng cách nào (muốn về màn Đăng nhập thì phải đăng xuất).

  Bốn lớp, tất cả bằng luật tất định (không thêm lượt gọi LLM nào):

  - **Điều kiện gắn với giá trị** (`intent.ANALYSIS_SYSTEM` luật 6): khi câu tester nêu giá trị
    thì `condition` phải ghi giá trị đó — *"chỉ khi chưa đăng nhập bằng tài khoản 0999"* thay vì
    *"chỉ khi chưa đăng nhập"*; và condition không được chép mật khẩu hay phần khác của câu.
  - **Guard trạng thái** (`composer.render_state_guards`): mục `KIỂM TRA TRẠNG THÁI` nay nói rõ
    *"điều kiện chỉ được coi là ĐÃ ĐÚNG khi quan sát thấy tài khoản đang đăng nhập LÀ «0999»;
    đang là giá trị KHÁC thì phải đạt «0999» trước khi bỏ qua việc này"*, kèm **cách quan sát**
    và **cách đổi** (`auth.current_account` → `auth.logout` rồi `auth.login_password`), và
    `NOT_VERIFIED` khi không quan sát được. Guard chỉ bắn khi tester **có nêu giá trị** — không
    có giá trị thì không khuyên đăng xuất vô cớ (điều kiện khi đó đúng là nhị phân).
  - **Điều kiện trước có cách đạt** (`composer.render_condition_achievers`): điều kiện nói về
    màn hình được ghép với workflow có `success_criteria` **chính là** trạng thái đó. Đo trên KB
    thật: khớp *mọi* trường cho 21 cặp nhưng chỉ **7 đúng (33%)** — sai điển hình là *phản
    nghĩa* ("Ứng dụng đang ở màn hình Đăng nhập" ↔ "Ứng dụng **rời** màn hình Đăng nhập", 0,86);
    khớp riêng `success_criteria` cho 9 cặp, **8 đúng**; sau chốt "chỉ điều kiện về màn hình",
    hàm này sinh **6 lời khuyên và cả 6 đều đúng**. Hạ ngưỡng xuống 0,75 là hỏng ngay.
  - **KB khai trạng thái có giá trị**: hai workflow mới đo trên máy thật —
    `auth.current_account` (Menu → khối trên cùng: tên, **số điện thoại**, hạng thành viên) và
    `home.current_house` (Trang chủ → góc trên trái: **tên nhà** + địa chỉ). Trạng thái khai
    trong `metadata.state_dimension` của chính workflow *quan sát* được nó (`id`, `label`,
    `value_hint`, `set_workflow`, `clear_workflow`), nên không có bảng nào phải đồng bộ: thêm
    ứng dụng mới là thêm dữ liệu KB, không sửa code. KB: 61 → 63 workflow.

  Kiểm chứng: 11 test mới; đã kiểm test **có răng** bằng cách tắt hai guard — **4 test fail**
  đúng như mong đợi. Chạy console 3 lần liên tiếp với câu thật: cả 3 lần điều kiện đều gắn giá
  trị, guard in đủ ba dòng (quan sát / đổi / `NOT_VERIFIED`), và bản ghép **không** rơi vào dự
  phòng (`status=llm`). Suite: 3099+ passed, 4 lỗi có sẵn không liên quan.

- **Prompt mang mật khẩu mà vẫn đi theo luồng "tài khoản đã lưu" (`auth.switch_account`).**
  Câu tester `đăng nhập tài khoản 0972842548 mật khẩu (Hoa21092003) chuyển nhà sang nhà Dled`
  có mật khẩu, nên cách làm đúng là `auth.login_password`; nhưng prompt sinh ra lại hướng dẫn
  agent *mở danh sách tài khoản đã lưu rồi chạm đúng dòng* — và **in kèm chính điều kiện trước
  của `switch_account`**: "Promt chỉ ghi mỗi tài khoản không ghi mật khẩu". Prompt vì thế vừa
  mang mật khẩu vừa nói "prompt không có mật khẩu": tự mâu thuẫn, và agent bị đẩy vào nhánh
  sai. Đo được **hai đường cùng dẫn tới lỗi**: lượt chọn cách làm do model đọc mô tả KB thì
  **không ổn định** (cùng câu, lần chọn đúng `login_password`, lần chọn `switch_account`), còn
  khi lượt đó trả JSON hỏng thì prepare rơi về **phân loại theo từ**, ở đó hai workflow hoà nhau
  và **thứ tự truy hồi** quyết định (`switch_account` truy hồi trước ⇒ nó thành cách làm chính).

  - `artemis/knowledge/intent.py`: `violated_precondition(workflow, prompt)` — điều kiện trước
    nói *không* cần thứ mà câu tester lại nêu thì workflow đó không thể là cách làm. Luật
    **không có danh sách từ khoá nghiệp vụ**: lấy cụm từ ngay sau một từ phủ định trong điều
    kiện trước ("… không ghi **mật khẩu**") rồi đối chiếu với câu tester.
  - `artemis/knowledge/prepare.py`: `_demote_violating_primaries` — chốt **tất định ở bước
    cuối**, áp cho *cả* đường model chọn và đường phân loại theo từ; cách làm chính vi phạm bị
    hạ xuống bối cảnh kèm lý do, và `_promote_without_violation` nâng ứng viên có điều kiện
    trước không vi phạm (không có ứng viên nào thì để thành "KB thiếu" cho agent quan sát UI,
    thay vì làm theo cách sai).
  - Kiểm chứng trên KB thật (61 workflow): câu có mật khẩu ⇒ **đúng 1** workflow bị chặn
    (`auth.switch_account`), `auth.login_password` thành cách làm chính; câu **không** mật khẩu
    ⇒ **0** bị chặn (luồng "tài khoản đã lưu" vẫn dùng được); các câu khác (bật công tắc, thêm
    thiết bị) ⇒ 0. Chạy console 3 lần liên tiếp: cả 3 đều `login_password` là chính, và điều
    kiện trước bị vi phạm không còn trong prompt. 4 test mới (2 ở `test_knowledge_intent.py`,
    2 ở `test_knowledge_prepare.py`); đã kiểm test có "răng" bằng cách tắt chốt — cả 2 test ở
    prepare fail đúng như mong đợi.
- **`apps/test_console/run.sh` bị lặp hai bản — nửa sau là code chết.** File chứa hai bản script
  nối nhau, bản đầu `exec … server.py` ở dòng 58 nên **mọi thứ sau đó không bao giờ chạy**: khối
  `LOCAL_LLM=1` và khối tunnel KB mới thêm đều vô hiệu (đo bằng `sh -x`: không có dòng nào của
  khối tunnel trong trace). Đã gộp thành một script (một `exec`), và `sh -x` xác nhận
  `Knowledge base: tunnel sẵn sàng (127.0.0.1:5433).` được in trước khi console khởi động.
- **Console không tự nhận KB sau khi tunnel lên.** `get_service()` dựng service **một lần** cho
  cả tiến trình, nên nếu lần dựng đầu rơi đúng lúc KB chưa tới được thì console báo "KB tắt" mãi
  cho tới khi ai đó khởi động lại — dù tunnel đã sẵn sàng. Nay nó dựng lại khi bản đang giữ
  không dùng được và đã hết cooldown 15 giây (cooldown để KB hỏng thật không bị thử ở mỗi
  request). Kiểm chứng đầu-cuối: console chạy với tunnel tắt (`degraded: true`) → mở tunnel,
  không restart → 18 giây sau `/api/kb/workflows` trả 61 workflow. 3 test mới
  (`tests/unit/test_knowledge_service_retry.py`).

- **Tunnel Knowledge base: `status` báo "đang chạy" trong khi cổng 5433 đã đóng.** Nguyên nhân
  gốc của việc KB "đứt" liên tục: `kb_tunnel.sh` chỉ kiểm tra **pid còn sống**, nên khi kết nối
  SSH nửa chết (VPS không tới được, ssh chưa kịp thoát) thì `status` vẫn nói "Tunnel đang chạy",
  còn mọi truy vấn KB đều `connection refused` — tester tưởng KB hỏng.  Đo thực tế: ssh pid còn,
  `nc -z 127.0.0.1 5433` đóng, `ping 103.199.18.222` mất 100% gói.

  - `scripts/kb_tunnel.sh`: `is_up` kiểm tra **cổng thật sự nhận kết nối** (`nc`/`lsof`), không
    chỉ pid; `kill_stale` dọn ssh cũ (pid còn nhưng cổng đóng) trước khi mở tunnel mới, nên
    `ExitOnForwardFailure` không còn làm tunnel mới thoát ngay; thêm lệnh `ensure` (idempotent,
    dùng cho script khởi động).
  - `apps/test_console/run.sh`: tự `ensure` tunnel trước khi chạy và in rõ
    `Knowledge base: tunnel sẵn sàng (127.0.0.1:5433).` — hoặc cảnh báo kèm đúng lệnh cần chạy.
    Tắt bằng `KB_TUNNEL=0` cho máy vai `server`.
  - `README.md`: mục 7 mới "Kết nối Knowledge base" — lệnh cho tester, điều kiện một lần cho
    mỗi máy (khoá SSH, biến `KB_VPS_*`), cách kiểm tra cổng, và bảng triệu chứng → nguyên nhân.

- **Prompt chỉ dẫn "chạm" trong khi thực tế phải "nhấn giữ" — agent nhảy bước và lặp.** Đo trên
  trace thật (prompt *bật tắt công tắc "Ổ cắm 12" 3 lần rồi vào màn hẹn giờ…*): KB ghi
  ``navigation.open_device_settings`` bước 1 là ``tap`` "Thẻ hoặc tên của thiết bị đích", nhưng
  trên máy **chạm thẻ thiết bị là bật/tắt**, phải **nhấn giữ ≥2 giây** mới mở màn chi tiết.
  Prompt trung thành với KB nên agent chạm → thiết bị đổi trạng thái (hai bước liên tiếp cùng
  "Chạm 'Ổ cắm 12'"), màn hình không sang cài đặt, và nó lặp lại thao tác.

  Sửa ở hai phía:

  - **KB (nguồn sự thật)** — ``navigation.open_device_settings``: bước 1 đổi thành
    ``long_press`` với thời lượng nêu rõ trong target ("NHẤN GIỮ ít nhất 2 giây"), thêm câu
    giải thích ngay trong mô tả ("CHẠM … là BẬT/TẮT — KHÔNG mở chi tiết"), bước bánh răng thành
    phương án dự phòng.  Thêm workflow ``device.card_tap_vs_hold`` nói rõ ngữ nghĩa thẻ thiết
    bị: chạm = bật/tắt, nhấn giữ ≥2s = chi tiết/cài đặt, kèm cảnh báo "chạm khi định vào cài
    đặt là lỗi đã gặp".  ``device.toggle_power`` sửa câu "mở thiết bị" mơ hồ thành "NHẤN GIỮ
    … để mở màn chi tiết".  (Agent đã hỗ trợ ``long_press`` kèm ``duration`` — không phải sửa
    code agent.)
  - **Composer** — ``_same_action`` nay so **tập** khái niệm thao tác phải *bằng nhau*, nên
    "nhấn giữ" và "chạm" **không** còn bị coi là cùng một thao tác (trước đây "nhấn" gộp cả
    hai, có thể làm mất bước nhấn giữ khi lọc trùng văn xuôi).  Mục điều kiện dừng thêm luật
    chống lặp: làm đúng thao tác KB ghi, không lặp lại y nguyên quá 2 lần, kiểm lại xem có
    phải thao tác khác (nhấn giữ thay vì chạm), vẫn không được thì dừng và báo chưa làm được.
  - ``labels_grounded`` bỏ báo oan: nhãn trích dẫn trong câu **của tester** ("Ổ cắm 12") là dữ
    liệu hợp lệ, không phải nhãn UI bịa — trước đó nó làm bản ghép bị từ chối và rơi về builder
    cũ.  3 test mới.

### Added

- **Điều kiện dừng & nhánh thất bại trong prompt.** Workflow trước đây chỉ sinh các bước
  thành công: không nói phải làm gì khi không tìm thấy phần tử hoặc thiếu dữ liệu bắt buộc,
  nên agent tự chọn mục khác hoặc quét lại danh sách vô hạn.

  - `artemis/knowledge/composer.py` — mục mới ``KHI KHÔNG THỰC HIỆN ĐƯỢC (điều kiện dừng)``
    (ngay sau các bước): bước chỉ bắt đầu khi kết quả mà bước trước khai đã quan sát được;
    bước tìm/chọn trong danh sách chỉ đi tiếp khi thấy **đúng** mục tiêu, không chọn mục thay
    thế, không tự đoán giá trị, không lặp quá 2 lần; hết danh sách mà không thấy ⇒ DỪNG và
    kết luận FAILED kèm lý do cụ thể (``NOT_FOUND``); **chưa** kiểm tra hết ⇒ ``NOT_VERIFIED``,
    không được kết luận là không có.  Bước tìm/chọn được gắn chốt ngay trên dòng của nó
    (``⟵ …``) để không phải suy ra từ mục khác.
  - Nhận diện "bước tìm/chọn" bằng **ngôn ngữ KB** (target/kết quả mong đợi nhắc tới một tập
    hợp nhiều mục) — không hardcode ứng dụng, và phần nói về danh sách chỉ in khi thật sự có
    bước loại đó nên prompt không phình.
  - Sửa hai trường KB đang bị **bỏ mất**: ``value`` (12 bước khai giá trị cần nhập — bỏ đi là
    agent tự đoán giá trị) và ``until`` (13 bước khai điều kiện dừng của vòng lặp cuộn).  Nay
    in thành ``(giá trị: …)`` và ``(lặp cho tới khi: …)``.
  - `validate_composed` thêm phép kiểm ``stop_conditions_present``: có bước tìm/chọn mà prompt
    thiếu một trong hai mã kết luận thì bản ghép bị coi là không đạt.
  - 6 test mới (`tests/unit/test_knowledge_composer.py`): tìm thấy, không tìm thấy (hết danh
    sách), chưa kiểm tra hết, nhận diện theo ngôn ngữ (kể cả câu tiếng Anh), app khác, và
    ``value``/``until`` vào prompt.

- **Prompt Composer tất định: bỏ lượt LLM viết lại prompt, ghép theo cấu trúc.** Nguyên nhân
  gốc của prompt lộn xộn/thiếu ý là **viết lại văn bản**: brief đã phân loại được đưa cho một
  lượt LLM "viết lại cho gọn", và lượt đó nuốt mất đúng phần cốt lõi (bước "mũi tên caret-down
  ở CUỐI ô nhập số điện thoại" rút thành "chọn tài khoản trong danh sách"), trộn thứ tự, lặp
  câu giữa các mục, và có lúc tự thêm thao tác không có trong KB.

  `artemis/knowledge/composer.py` (mới) ghép prompt bằng luật, năm mục theo thứ tự:
  ``NHIỆM VỤ`` → ``KIỂM TRA TRẠNG THÁI`` → ``CÁC BƯỚC THỰC HIỆN`` → ``LƯU Ý QUAN TRỌNG`` →
  ``XÁC MINH KẾT QUẢ`` (mục nào không có nội dung thì bỏ). Mỗi việc trong phần bước là một
  tiêu đề nhỏ ghi **workflow nguồn** (truy vết), các bước **đánh số liên tục trong việc đó**,
  kèm điều kiện hoàn thành của từng bước. ``normalize_workflow`` chuẩn hoá mỗi workflow thành
  ``preconditions → actions → expected_results → verification``, giữ nguyên thứ tự bước của KB
  (quan hệ phụ thuộc) thay vì thứ tự truy hồi; điều hướng tới màn hình đứng trước vì đó là phụ
  thuộc trạng thái. ``expected_results`` thẳng hàng theo chỉ số với ``actions`` (lệch một bước
  là gán sai điều kiện hoàn thành).

  Ba luật lọc, mỗi luật có điều kiện rõ: (1) câu gần trùng ⇒ bỏ; (2) câu **có điều kiện** là
  bản đầy đủ hơn của câu mệnh lệnh **về cùng một thao tác** ⇒ giữ câu có điều kiện — và luật
  này **không áp cho danh sách bước** (hai bước liên tiếp cùng là "tap" vẫn là hai thao tác;
  áp vào đó từng làm mất một bước); (3) xung đột chỉ báo khi **trích được hai câu** mâu thuẫn
  (cùng câu chữ mà ngược khẳng định), kèm bằng chứng, và giữ cả hai thay vì tự chọn.

  Chỗ KB thiếu (không có bước, không có cách mở màn hình) thì phát ra đúng một câu "quan sát
  UI để xác định…", và quy tắc "không tự suy ra vị trí nút/biểu tượng/đường điều hướng" được
  đặt ở **đầu prompt** nên Planner nhận cùng ràng buộc như Operator.

  Chi phí: **0 lượt LLM thêm** — lượt viết lại bị bỏ, bước "chọn cách làm" đọc mô tả KB vẫn là
  1 lượt, nên tổng vẫn 2 lượt (hiểu yêu cầu + chọn cách làm). Đo trên KB thật: prompt
  ``đăng xuất và đăng nhập tài khoản 0972842548`` dài ~1,6k ký tự, ghép trong ~8 ms,
  ``checks`` không có mục nào trượt.

### Fixed

- **Prompt tester đã duyệt không được gửi cho agent — và không ai thấy điều đó.** Đã xảy ra
  thật: tester bấm "Chuẩn bị prompt", xem bản đã ghép kiến thức (có cả
  ``navigation.open_menu``), rồi bấm "Chạy test" mà **không** bấm "Dùng bản này". Agent nhận
  đúng câu gốc **43 ký tự**, không có KIẾN THỨC nào ⇒ Planner tự bịa bước điều hướng
  ("tap profile icon ở góc trên bên phải"), Object Detector xác nhận vật thể không tồn tại
  với `confidence=1.00`, cú tap trả `ui_changed=False`. Kiểm chứng trên trace: log chỉ ghi
  `artemis run đăng xuất và đăng nhập tài khoản 0972842548 …`, và **mọi** lượt chạy gần đây
  đều có `goal_sent` = câu gốc 43 ký tự — kể cả lượt được coi là "thành công".

  Ba sửa đổi, không đổi hành vi mặc định:

  - `apps/test_console/server.py` — `remember_prepared_prompt` ghi nhớ bản vừa chuẩn bị
    (từ `/api/knowledge/prepare` và `/api/knowledge/context`); `forgotten_prepared_prompt`
    sinh cảnh báo khi lượt chạy không có `prompt_override` mà console biết mình vừa ghép
    kiến thức cho đúng câu đó. Cảnh báo đi theo kênh `warnings` sẵn có nên hiện thành toast
    và vào log task. Câu gốc được khớp sau khi chuẩn hoá khoảng trắng/hoa-thường; bản "chuẩn
    bị" trùng câu gốc (KB không ghép được gì) thì không ghi nhớ.
  - `apps/test_console/static/index.html` — dòng `#runPromptState` nói rõ lượt chạy sắp tới
    gửi prompt **gốc** hay **đã chuẩn bị** (kèm số ký tự); cờ `promptOverride` được lưu vào
    `localStorage` khoá theo câu gốc, nên tải lại trang (hoặc khởi động lại console) không
    còn làm mất bản tester đã duyệt như trước.

  8 test mới (`tests/unit/test_console_run_prompt.py`), gồm cả kiểm chứng đầu-cuối qua
  `TestClient`: chưa chuẩn bị → im lặng; đã chuẩn bị mà chạy câu gốc → cảnh báo; chạy bằng
  bản đã duyệt → im lặng và `goal_sent` có kiến thức.

### Added

- **Tầng điều hướng: workflow chính được nối với hướng dẫn *đi tới màn hình* nó cần.**
  Lỗi gốc: KB nói "'Đăng xuất' nằm ở màn hình Menu, ~73% chiều cao, đã hiện sẵn" nhưng
  **không nói cách mở màn Menu** — nên Planner tự bịa ra bước "navigate to account
  settings/profile", Operator bịa tiếp vị trí ("profile icon in the top right"), Object
  Detector "xác nhận" vật thể không tồn tại với `confidence=1.00`, và cú tap cuối cùng trả
  `ui_changed=False`. Ba tầng bịa nối tiếp nhau vì prompt thiếu đúng một câu.

  Quy ước dữ liệu: namespace ``navigation.*`` = "hướng dẫn đi vào một màn hình"
  (``navigation.open_menu`` = "làm sao tới màn Menu"). Hai thay đổi:

  - `artemis/knowledge/prepare.py` — `_attach_navigation_knowledge`: với mỗi workflow
    PRIMARY, rút ra **tên màn hình** nó nhắc tới (description, preconditions,
    `steps[].expected_state`, `steps[].target`) rồi phát một truy vấn KB theo *màn hình*
    (``mở màn hình Menu``). Chỉ nhận hit thuộc namespace điều hướng và `trusted` — cùng
    luật bằng chứng như mọi hit khác. Lỗi ở bước này không làm hỏng prepare.
    Vì sao phải truy vấn riêng: truy hồi *theo task* không đáng tin cho việc này — đo trên
    KB thật, ``xóa phòng`` không trả về navigation nào, còn ``đăng xuất`` có trả
    ``navigation.open_menu`` nhưng ở vai trò phụ và không được render.
  - `artemis/knowledge/intent.py` — `screen_phrases` / `needed_screens` / `navigation_query`
    / `render_navigation_knowledge`, và `build_knowledge_brief` render workflow điều hướng
    vào mục **KIẾN THỨC** (kèm **bước**) thay vì mục bối cảnh. Đây là chỗ duy nhất bước của
    một workflow được phép vào prompt, và là lý do có hàm render riêng: với workflow chức
    năng, bước bị giấu để agent tự tìm phần tử trên UI hiện tại; với ``navigation.*`` thì
    bước *chính là* câu trả lời, giấu đi là bỏ hết thông tin.

  Bộ rút tên màn hình phải siết ba lần vì KB viết màn hình lẫn trong câu: lần đầu nó sinh
  ra những "màn hình" như ``và ĐÃ HIỆN SẴN — không cần cuộn``, ``hiện tại không``,
  ``thì tap NGAY`` (mỗi cái tốn một truy vấn KB vô ích). Luật hiện tại: đuôi sau "màn hình"
  phải viết hoa (tên riêng), ≤ 4 từ, và không chứa dấu câu/từ khẳng định.

  Đo trên KB thật (31 → 37 workflow sau khi import 7 workflow ``navigation.*``): prompt
  ``đăng xuất và đăng nhập tài khoản 0972842548`` nay chứa
  ``navigation.open_menu: tap Tab Menu ở ngoài cùng bên phải của thanh điều hướng dưới cùng
  → Màn hình Menu và thông tin tài khoản xuất hiện``, chỉ **một** workflow điều hướng được
  nối (không kèm ``open_rooms``/``open_home``/``open_collections``), và 2 workflow chính
  vẫn giữ nguyên. 12 test mới; toàn bộ unit suite 3066 passed.

### Changed

- **Đổi đường đi của LLM agent sang Google trực tiếp: nhanh hơn ~3,8× mỗi lượt và task
  vượt được ngân sách (đo 27/10/2026).** Nút thắt độ trễ **không phải model** mà là gateway
  OpenAI-compatible (`sep21th03.com/artemis/llm/v1` → `api.vilao.ai`). Cùng một model
  `gemini-3.5-flash-lite`, cùng client của app, chỉ khác đường đi:

  | prompt | gateway `openai:ts/gemini-3.5-flash-lite` | Google `google:gemini-3.5-flash-lite` |
  |---|---|---|
  | ~50 token | 4,65s / **38,03s** | **0,95s / 0,92s** |
  | ~5.000 token | **38,08s** / 4,33s | **1,35s / 1,40s** |
  | ~15.000 token | **56,85s** / 8,61s | **1,62s / 1,39s** |
  | + 1 ảnh chụp thật | **>200s** (không trả về) | **1,69s / 2,92s** |

  Gateway có **hai cực** (~4,5s hoặc ~38s) và **không phụ thuộc kích thước prompt** (prompt
  50 token cũng dính 38s) — tức phần lớn thời gian là xếp hàng/tầng vận chuyển, không phải
  model suy nghĩ. Google trực tiếp gần như **phẳng** theo kích thước (50 → 15k token chỉ
  +0,5s). Đúng như trace cũ cho thấy: `operator_llm` trung vị 10,56s, max 198,81s.

  Hệ quả trên một task thật (`pro`, `đăng xuất và đăng nhập tài khoản 0972842548`): trước đây
  hết ngân sách 480s mà chưa xong; nay **thành công trong 294,8s**, 70 lượt LLM, `operator_llm`
  trung vị **2,80s** (min 1,04s, max 7,54s). Planner 1,66s/5,96s, checker 1,78s/3,50s.

  Lợi thêm: `thinking_level` trong `config/artemis.jsonc` **chỉ có tác dụng khi
  `provider=google`**, nên trước đây `low` (operator) và `high` (planner) bị bỏ qua. Đo trên
  `gemini-3.5-flash`: mặc định 7,25s → `thinking_level=low` còn **1,54s**, đáp án vẫn đúng.

  Cấu hình: bốn node `operator` / `explorer` / `planner` / `checker` dùng
  `GEMINI35FLASH_*_PROVIDER=google` với tên model **bỏ tiền tố `ts/`** (gửi `ts/...` sang
  Google là 404 — đúng lỗi của bộ nén ngữ cảnh ở mục Fixed). `hopper` và `summarizer` giữ
  nguyên đường gateway vì chúng chỉ đọc chữ và chạy thưa. Lưu ý: `gemini-2.5-flash` trong
  ghi chú `.env` nay đã **404 "no longer available"**, và bản checkout MCP
  (`/Users/nguyenhoa/Developer/artemis`) không có cơ chế `${env:...}` nên không đổi được
  bằng biến môi trường.

### Fixed

- **Bộ nén ngữ cảnh gọi thẳng Google nên 404 ở mọi lượt — nay đi qua router đã cấu hình.**
  `artemis/memory/chunking.py` (StepCapsuleLens) và `artemis/agents/flash/summarizer.py`
  gọi `get_google_llm(model_name=...)`, mà hàm đó **ép** `provider=google`, trong khi tên
  model lấy từ `config/artemis.jsonc` là tên của gateway OpenAI-compatible
  (`ts/gemini-3.5-flash-lite`). Endpoint Google native không biết tên đó ⇒ 404 (đo được
  trước đây: một lần chạy Pro có 60 lượt vision, tất cả 404). Tệ hơn, nhánh `except` của
  summarizer còn **âm thầm** rơi về Google, và `FlashRunner._init_llm` hardcode
  `get_google_llm("gemini-2.5-flash")` — tức đổi provider mà người vận hành không cấu hình.

  Nay có `artemis.services.llm.get_routed_llm(ctx, role=..., model_name=...)`: provider /
  endpoint / khoá lấy từ cấu hình, `model_name` chỉ ghi đè *tên model*, và lỗi ⇒ trả `None`
  để bên gọi **giữ nguyên context chưa nén** (không raise, không tự đổi provider). Lens nén
  ghi lý do vào performance log thay vì im lặng; `render()` trả `None` (không có capsule)
  khi không resolve được model, nên tác vụ chính không bị ảnh hưởng.

- **Performance log nay nói được "token đi đâu".** Mỗi lượt gọi LLM mang thêm
  `context_breakdown` (số ký tự theo nguồn: system / quan sát / lịch sử + số ảnh), và mỗi
  lượt nén ghi `stage=compression` với token trước/sau, thời lượng, lỗi và lý do fallback.
  Token trong các trường đó là **ước lượng (chars/4)** và được ghi rõ, tách khỏi token do
  provider báo cáo.

### Added

- **Lớp đo hiệu năng (Performance monitoring) — chỉ quan sát, không tối ưu.** Mục tiêu: khi
  agent đứng yên 20 giây, nhìn vào Web Console là biết nó đang chờ Planner, Operator, LLM
  API, retry, ADB, screenshot hay đang lặp.

  - `artemis/perf/` (mới) — span đo bằng `time.perf_counter()`, timestamp thực bằng
    `time.time()`, ghi JSONL vào `traces/<session_id>/perf_events.jsonl`.  Bốn nhóm được đo
    ở đúng điểm chốt: **ADB** (`AndroidAdbDriver`: tap/swipe/input/key/launch/stop/shell và
    từng pha của một lần quan sát màn hình), **LLM**
    (`RobustChatModelWrapper.ainvoke/complete`: model/provider/endpoint, token, TTFT,
    retry/backoff, vai trò Planner/Operator/Checker/Summarizer), **node agent + tool**
    (decorator `trace` của DataEngine), **RAG** (`retrieve_for_run`, cả đường local lẫn
    remote, tách embedding/vector search theo `timings_ms` mà service đã đo sẵn).
  - **Recorder mở trong tiến trình thực thi** (`DataEngine.start_session`), không phải trong
    console: tiến trình lái điện thoại là Artemis Daemon, nên chỉ ở đó mới đo được.
    `allocate_step_id` gắn `step_id` vào ngữ cảnh để timeline nhóm theo bước.
  - **Tab Performance** trong Mobile Test Console: bảng công đoạn (Calls / Total / Avg / Max
    / Errors / % Runtime), timeline từng bước mở rộng được (Request ID, retry, HTTP, TTFT,
    lỗi, UI trước/sau, thời gian chờ giữa các bước), khối nhận định bottleneck, nút **xuất
    JSON**.  Số liệu hiện ra **trong lúc agent đang chạy**: console đọc lại file perf của
    đúng session mỗi 3 giây.
  - **Exclusive time, không cộng trùng.** Bảng cộng theo thời gian *riêng* của từng span
    (đã trừ span con), nên `Operator LLM` không bị tính hai lần bên trong node Operator.
    Idle = phần wall-clock không có span nào chạy.  Nếu các công đoạn chạy song song
    (screenshot + hierarchy) làm tổng vượt 100%, báo cáo **nói rõ** thay vì im lặng.
  - **Không đo được thì N/A, không gán 0** — mọi trường số đo là optional; provider không
    trả token thì bảng ghi `N/A`.  Cùng nguyên tắc cho các con số đếm khi session chưa có
    sự kiện nào.
  - **Không rò dữ liệu**: `redact()` chạy trên mọi payload trước khi ghi (api key,
    authorization, password, token → `<redacted>`; chuỗi dài như prompt/ảnh base64 → độ
    dài).  Nội dung gõ vào ô nhập cũng chỉ ghi **độ dài** (có thể là mật khẩu).
  - **Phát hiện "không tiến triển" có bằng chứng**: chuỗi hành động lặp cùng chữ ký (toạ độ
    làm tròn 10px) mà UI không đổi, dựa trên `ui_fingerprint` (hash của hierarchy).  Màn
    hình không đổi **một lần** không bị coi là lỗi — nó chỉ là một số đo.
  - Cấu hình: `ARTEMIS_PERF_ENABLED` (mặc định bật), `ARTEMIS_PERF_LEVEL`
    (`full`/`summary`/`off`), `ARTEMIS_PERF_UI_POLL_MS`.
  - Test: `tests/unit/test_perf_recorder.py` (7 ca), `test_perf_aggregate.py` (16 ca),
    `test_perf_instrumentation.py` (9 ca — chạy trên driver ADB thật với device giả),
    `test_console_perf_api.py` (6 ca), `test_console_perf_ui.py` (10 ca).  Có ca đo
    **overhead** của lớp đo (chênh lệch có/không monitoring) và ca bảo đảm không ghi nội
    dung đã gõ.

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
  - Test: `tests/unit/test_knowledge_intent.py` (28 ca — gồm ca mục kiến thức phải rút gọn
    để *còn* khi hạn mức chật), `tests/unit/test_prepare_cases.py`
    (34 ca — đúng sáu ca bắt buộc), `tests/unit/test_knowledge_prepare.py` (40 ca cơ chế —
    gồm ca bản LLM bỏ "cách làm" thì bị từ chối), `tests/unit/test_console_prepare_api.py`
    (36 ca), `tests/unit/test_console_prepare_ui.py`
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

- **Mục `KIẾN THỨC ỨNG DỤNG` bị bỏ *toàn bộ* khi hạn mức kiến thức chật — nên bản tổng hợp
  viết chung chung và chi tiết "chọn tài khoản đã lưu" không bao giờ tới được agent.**
  Đo trên KB thật, prompt `đăng xuất và đăng nhập tài khoản 0972842548`, hai workflow
  (`auth.logout`, `auth.switch_account`): `build_knowledge_brief` cắt *theo mục*, mà kế
  hoạch + tiêu chí đã chiếm hết ngân sách 1200 ký tự, nên **brief gửi cho LLM chỉ còn 384
  ký tự và không có mục kiến thức nào**. LLM chỉ còn kế hoạch để viết, và nó viết
  "`auth.switch_account: thực hiện đăng nhập tài khoản 0972842548`" — nghe hợp lý nhưng
  agent sẽ đi tìm ô nhập tài khoản và gõ tay, trong khi đường đúng là **mở danh sách tài
  khoản đã lưu rồi chọn**. Bước của workflow cố tình không vào prompt, nên *câu đầu phần
  Mô tả* là chỗ duy nhất nói cách làm.

  Nay mục kiến thức được **rút gọn để còn** thay vì bị bỏ
  (`compact_knowledge_lines`): giữ mọi dòng tiêu đề workflow (id + tên) trước, bỏ dòng
  tiêu chí (đã có nguyên vẹn ở mục `TIÊU CHÍ XÁC MINH`), rồi cắt bớt từng dòng chi tiết.
  Trần mặc định nâng **1200 → 2400** vì 1200 không đủ cho hai workflow thông thường.
  Đo lại cùng prompt: brief có đủ kiến thức, và bản LLM trả về đúng chi tiết cần —
  *"Mở danh sách tài khoản đã từng đăng nhập trên máy này rồi chọn một tài khoản để đăng
  nhập… chọn xong chỉ cần bấm Đăng nhập, không phải gõ mật khẩu"*.

- **Kiểm tra `primary_knowledge_kept` không hề soi phần "cách làm".** Nó chỉ so điều kiện
  trước, tiêu chí và cảnh báo, nên một bản LLM bỏ hẳn mô tả vẫn được coi là đạt — đúng ca
  trên: độ phủ của mô tả trong bản LLM là **0.49**, dưới ngưỡng 0.5, nhưng không tiêu chí
  nào bắt được. Nay `first_sentence(description)` của workflow THỰC HIỆN nằm trong danh
  sách kiểm (chỉ câu đầu: phần sau là ghi chú đo đạc, đòi cả đoạn sẽ từ chối oan), và quy
  tắc 8 trong chỉ dẫn cho LLM nói rõ phải giữ "cách làm". Trượt ⇒ **dự phòng**, và bản dự
  phòng vẫn nói được cách làm nên tester không mất thông tin.

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
