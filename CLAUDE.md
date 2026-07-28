# CLAUDE.md

Hướng dẫn cho Claude Code khi làm việc trong repo này.

## Dự án là gì

Bài Lab 03 (VinUni × GDGoC) so sánh **Chatbot (Cấp 2)** với **ReAct Agent (Cấp 3)**.
Đề tài đã chọn: **Trợ Lý Duyệt Chi Phí Doanh Nghiệp** — xử lý trọn vòng đời hóa đơn:

```
Ảnh hóa đơn → OCR (HTTP, máy khác trong LAN) → RAG tra policy nội bộ
  → đối chiếu tuân thủ → kiểm tra trùng → chuyển khoản → ghi SQLite → báo cáo tuần/tháng/quý
```

Thiết kế chi tiết và lý do lựa chọn nằm ở [brainstorm.md](brainstorm.md). Đọc file đó trước
khi sửa kiến trúc.

## Lệnh hay dùng

```bash
# Kiểm tra từng tầng độc lập (mỗi file có smoke test riêng ở __main__)
python src/database.py      # tạo + seed DB, in thử báo cáo
python src/rag.py           # test BM25 tra policy
python src/bank_api.py      # test các kịch bản giao dịch
python src/ocr_client.py    # ping service OCR + test parser
python src/tools.py         # smoke test 11 tool, cả happy path lẫn error path
python src/prompts.py       # kiểm tra prompt sinh ra + rò rỉ tool theo vai
python src/graph.py         # test parser ReAct + xuất sơ đồ graph

# Chạy app
python src/app.py --all --mode both --save-trace docs/trace_run_log.md
python src/app.py --case 5 --role ketoan --user KT-01
python src/app.py --interactive
streamlit run src/ui_streamlit.py

# Tiện ích
python src/app.py --reset-db          # xoá và seed lại DB
python src/app.py --check-ocr         # kiểm tra kết nối OCR
python src/app.py --audit-blocked     # xem các hành động bị chặn
python src/app.py --export-flowchart  # sinh lại docs/hybrid_flowchart.mermaid
```

## Kiến trúc

| File | Vai trò |
| :--- | :--- |
| `src/providers.py` | Adapter đa LLM (Gemini/OpenAI/Anthropic/OpenRouter/Mock). **Không sửa** — có sẵn từ boilerplate. |
| `src/roles.py` | 2 vai trò + ma trận phân quyền 11 tool (lớp phòng thủ 2) |
| `src/database.py` | SQLite 4 bảng: `invoices`, `payments`, `tickets`, `audit_log` |
| `src/rag.py` | BM25 thuần Python trên `data/policies/*.md`, trả kèm trích dẫn nguồn |
| `src/ocr_client.py` | HTTP client tới service OCR, tự dò endpoint |
| `src/bank_api.py` | Cổng chuyển khoản giả lập, có lỗi chủ đích |
| `src/tools.py` | 11 tool + `TOOL_SPECS` + registry lọc theo vai (lớp phòng thủ 4) |
| `src/prompts.py` | Baseline prompt, ReAct prompt sinh động theo vai, hằng số Guardrails |
| `src/graph.py` | LangGraph 6 node + **parser ReAct tự viết** |
| `src/app.py` | CLI, chatbot baseline, chạy test cases, xuất trace markdown |
| `src/ui_streamlit.py` | 2 giao diện web theo vai trò |

## Quy ước bắt buộc khi sửa code

**Tool luôn trả `str`, không bao giờ `raise`.** Lỗi nghiệp vụ là *dữ liệu* để Agent suy
luận đổi hướng, không phải sự cố làm sập chương trình. Thông báo lỗi phải nói rõ Agent
phải làm gì tiếp theo, không chỉ nói "sai tham số".

**Không dùng `create_react_agent()` của LangGraph.** LangGraph ở đây chỉ điều phối node và
edge. Phần parse `Thought:`/`Action:`, cắt `Observation:` do LLM bịa, dispatch tool đều là
code tự viết trong `graph.py` — vì rubric chấm trực tiếp vòng lặp ReAct. Dùng hàm dựng sẵn
là mất điểm tiêu chí 2 (30%).

**Bốn lớp phòng thủ, không được bỏ lớp nào:**
1. `node_moderation` — chặn prompt injection ở cửa vào
2. `roles.py` + `get_tools_for_role()` — lọc tool theo vai *trước khi* đưa vào prompt
3. Vòng lặp — `MAX_ITERATIONS`, chống lặp hành động, cắt Observation bịa, dừng khi provider lỗi
4. `tools.py` — `transfer_payment` tự kiểm tra lại chính sách + `node_human_approval`

Lớp 4 là lớp duy nhất prompt injection không vượt được vì nó là Python thuần. Đừng bao giờ
để một quy tắc an toàn chỉ tồn tại trong system prompt.

**Cờ phê duyệt nằm ngoài tham số tool.** `tools.grant_approval()` ghi vào `_SESSION`, LLM
không chạm tới được. Nếu chuyển nó thành tham số hàm, LLM chỉ cần bịa `approved_by="KT-01"`
là vượt cửa.

**Prompt không được nhắc tên tool mà vai đó bị cấm.** Nhắc tới là đã rò rỉ sự tồn tại của
nó. `python src/prompts.py` có bài test tự động kiểm tra điều này — chạy lại sau mỗi lần
sửa `_REACT_TEMPLATE`.

**Thêm tool mới** phải cập nhật đồng thời 3 chỗ: hàm trong `tools.py`, mục trong
`TOOL_SPECS` (prompt sinh tự động từ đây), và `ROLE_TOOL_PERMISSIONS` trong `roles.py`.

## Cấu hình

Chép `.env.example` thành `.env` rồi điền:

- `LLM_PROVIDER=gemini` + `GEMINI_API_KEY=...`
- `OCR_BASE_URL=http://<IP-máy-chạy-OCR>:8080` — để `OCR_ENDPOINT` trống thì client tự dò
- `BANK_FAIL_RATE=0.1` — đặt `0` khi cần demo mượt không bị timeout ngẫu nhiên

## Không commit

`.env` · `data/expenses.db` · ảnh trong `data/invoices/` (là **PII**) · `docs/trace_run_log.md`.
Đã cấu hình sẵn trong `.gitignore` — kiểm tra bằng `git status` trước khi push.

## Dữ liệu mẫu

`database.py` seed 20 hóa đơn rải Q1–Q3/2026, 15 giao dịch, 3 phiếu đề nghị. Hóa đơn
`HD-2026-0016` đến `HD-2026-0020` cố ý để trạng thái `NEW`/`PENDING` làm nguyên liệu demo.
`HD-2026-0013` đã `PAID` — dùng cho test case chống trùng.

`data/policies/*.md` là kho tri thức RAG, viết theo văn phong quy chế doanh nghiệp có đánh
số Điều. Sửa nội dung thì `rag.reload_index()` sẽ tự nạp lại theo heading `##`.
