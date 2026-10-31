# Đóng góp cho HunonicTest

Cảm ơn anh/chị đã muốn đóng góp cho **HunonicTest**. Mọi hình thức đều được hoan nghênh: báo lỗi, đề
xuất tính năng, sửa tài liệu hay gửi code.

---

## 🤝 Cách đóng góp

- **🐛 Báo lỗi & đề xuất tính năng**: mở một **Issue** trong repo này.
- **💡 Gửi Pull Request**:
  1. Tạo nhánh cho tính năng/sửa lỗi (`git checkout -b feat/my-feature`).
  2. Sửa code rồi tự kiểm bằng `make test`, `make lint` và `make typecheck`.
  3. Mở Pull Request kèm mô tả ngắn về việc đã làm.
- **📖 Tài liệu**: hướng dẫn cài đặt – cấu hình – chạy nằm ở [README.md](./README.md); tài liệu của
  Test Console ở [`apps/test_console/README.md`](./apps/test_console/README.md). Sửa code xong thì cập
  nhật luôn tài liệu liên quan và thêm mục vào [CHANGELOG.md](./CHANGELOG.md).

---

## Test layers

`make test` is the required, deterministic suite. It does not need an Android
device, model credentials, or the optional private cloud service. This is the
same suite used by pull-request CI.

- `make test-integration` runs cross-component tests that may need configured
  model credentials.
- `make test-device` runs Android and end-to-end tests and therefore requires
  an attached, authorized device or emulator.
- `make test-all` collects every test tree and is intended for a fully
  provisioned maintainer environment.

Tests that require external state must carry the appropriate `android`,
`cloud`, `manual`, or `e2e` marker. They must remain safely collectable when
that dependency is unavailable.
