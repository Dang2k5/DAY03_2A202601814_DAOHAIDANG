# Sơ đồ hệ thống — Trợ Lý Duyệt Chi Phí Doanh Nghiệp

Lab 03 · VinUni × GDGoC · Đề tài #8

Tài liệu này tách thành **4 sơ đồ độc lập + 1 bảng ánh xạ**, mỗi phần trả lời
đúng một câu hỏi. Trước đây tất cả nằm chung một hình 62 node nên rối; giờ đọc
từ trên xuống theo thứ tự tổng quan → chi tiết.

| # | Phần | Trả lời câu hỏi |
| :-: | :--- | :--- |
| 1 | [Luồng tổng quan](#1--luồng-tổng-quan--khi-nào-chatbot-khi-nào-agent) | Khi nào đi Chatbot, khi nào đi ReAct Agent? |
| 2 | [Vòng lặp ReAct](#2--vòng-lặp-react-bên-trong-agent-path) | Bên trong Agent path diễn ra gì? |
| 3 | [4 lớp phòng thủ](#3--4-lớp-phòng-thủ-guardrails) | Hệ thống chặn hành vi sai ở đâu? |
| 4 | [Tầng công cụ & dữ liệu](#4--tầng-công-cụ--tầng-dữ-liệu) | 11 tool đọc/ghi vào đâu? |
| 5 | [Ánh xạ rubric](#5--ánh-xạ-sang-rubric-chấm-điểm) | Mỗi tiêu chí chấm điểm nằm ở file nào? |

> **Cách xem:** VS Code cài extension *Markdown Preview Mermaid Support*, hoặc dán
> từng khối vào <https://mermaid.live>.
>
> **Phân biệt với `hybrid_flowchart_mermaid.md`:** file đó **sinh tự động** từ
> `graph.get_graph().draw_mermaid()` — chỉ có node và edge trần, dùng để chứng minh
> sơ đồ khớp code thật. File này **vẽ tay**, bổ sung tầng công cụ, tầng dữ liệu,
> guardrails và ánh xạ rubric mà bản tự sinh không thể hiện được.

---

## 1 · Luồng tổng quan — khi nào Chatbot, khi nào Agent

Đây là **Hybrid Decision Flowchart** của tiêu chí 5. Mọi câu hỏi đều đi qua đúng
một trong ba nhánh: bị chặn, trả lời thẳng, hoặc vào vòng lặp Agent.

```mermaid
flowchart TB
    START(["Câu hỏi người dùng<br/>+ vai trò + mã nhân viên"])

    MOD{"LỚP 1 · node_moderation"}
    REFUSE["Từ chối lịch sự<br/>MODERATION_BLOCKED"]

    SV{"node_supervisor<br/>phân vân thì chọn REACT"}

    CB["🤖 CHATBOT · Cấp 2<br/>đúng 1 LLM call<br/>tool_calls = 0"]
    RA["🧠 REACT AGENT · Cấp 3<br/>vòng lặp có tool<br/>→ Sơ đồ 2"]

    TRACE["Trace có cấu trúc<br/>steps · tool_calls<br/>iterations · stop_reason"]
    ENDN(["Trả lời người dùng"])

    START --> MOD
    MOD -- "INJECTION / ABUSE" --> REFUSE
    MOD -- "SAFE" --> SV
    SV -- "DIRECT<br/>câu hỏi lý thuyết,<br/>không cần dữ liệu công ty" --> CB
    SV -- "REACT<br/>cần dữ liệu thật<br/>hoặc hành động thật" --> RA

    REFUSE --> TRACE
    CB --> TRACE
    RA --> TRACE
    TRACE --> ENDN

    classDef guard fill:#F5E6E3,stroke:#B23A32,stroke-width:2px,color:#8E2C25
    classDef stop fill:#FBEFED,stroke:#B23A32,color:#8E2C25
    classDef chatbot fill:#E8EEF5,stroke:#3A6699,stroke-width:2px,color:#274766
    classDef react fill:#E3EFE8,stroke:#2E6B4F,stroke-width:2px,color:#1F4A36
    classDef store fill:#F6EDDC,stroke:#9A6B1F,color:#6B4A14
    classDef terminal fill:#171A1F,stroke:#171A1F,color:#FFFFFF

    class MOD,SV guard
    class REFUSE stop
    class CB chatbot
    class RA react
    class TRACE store
    class START,ENDN terminal
```

**Tiêu chí định tuyến** — `node_supervisor` kết hợp luật cứng và LLM:

| Đi CHATBOT khi | Đi REACT AGENT khi |
| :--- | :--- |
| Hỏi khái niệm kế toán, thuế, quy định chung | Có đường dẫn ảnh hóa đơn cần OCR |
| Câu trả lời không phụ thuộc dữ liệu công ty | Cần tra chính sách nội bộ kèm trích dẫn |
| Không có hành động cần thực thi | Cần truy vấn / ghi database, chuyển khoản, xuất báo cáo |

---

## 2 · Vòng lặp ReAct bên trong Agent path

Phần này là **code tự viết** trong `src/graph.py`, không dùng
`create_react_agent()` — vì rubric tiêu chí 2 chấm trực tiếp vòng lặp.

```mermaid
flowchart TB
    IN(["Vào từ Sơ đồ 1"])

    CALL["node_call_llm<br/>prompt sinh động theo vai<br/>+ scratchpad các bước trước"]
    PARSE{"parse_llm_output<br/>gỡ code fence<br/>✂️ CẮT Observation do LLM tự bịa"}
    PACT{"parse_action<br/>tách tên tool + tham số<br/>vá 4 kiểu sai cú pháp"}
    HINT["Chèn gợi ý sửa lỗi<br/>MALFORMED_ACTION_HINT<br/>NO_ACTION_HINT"]

    GUARD["Qua 4 lớp phòng thủ<br/>→ Sơ đồ 3"]
    EXEC["execute_tool<br/>→ Sơ đồ 4"]
    OBS["Observation THẬT<br/>ứng dụng chèn vào scratchpad"]

    LOOP{"Còn budget?<br/>MAX_ITERATIONS = 10"}
    FIN(["FINAL_ANSWER<br/>bắt buộc trích số liệu<br/>từ Observation"])
    EXH(["Safe Fallback<br/>MAX_ITERATIONS"])

    IN --> CALL
    CALL --> PARSE
    PARSE -- "có Final Answer" --> FIN
    PARSE -- "không có Action<br/>lẫn Final Answer" --> HINT
    PARSE -- "có Action" --> PACT
    PACT -- "sai cú pháp" --> HINT
    HINT --> CALL
    PACT -- "hợp lệ" --> GUARD
    GUARD --> EXEC
    EXEC --> OBS
    OBS --> LOOP
    LOOP -- "còn" --> CALL
    LOOP -- "hết" --> EXH

    classDef guard fill:#F5E6E3,stroke:#B23A32,stroke-width:2px,color:#8E2C25
    classDef react fill:#E3EFE8,stroke:#2E6B4F,stroke-width:2px,color:#1F4A36
    classDef terminal fill:#171A1F,stroke:#171A1F,color:#FFFFFF

    class PARSE,PACT,LOOP,GUARD guard
    class CALL,HINT,EXEC,OBS react
    class IN,FIN,EXH terminal
```

**4 nguyên tắc bất biến của ReAct** (CODELAB §4) hiện thực ở đâu:

| # | Nguyên tắc | Hiện thực |
| :-: | :--- | :--- |
| ① | Không lặp vô hạn | `MAX_ITERATIONS = 10` + `MAX_REPEATED_ACTIONS = 2` |
| ② | Mỗi Action đúng 1 Observation | `parse_llm_output` cắt bỏ mọi thứ từ `Observation:` trở đi |
| ③ | Observation quay lại prompt | scratchpad nối dồn qua từng vòng, đưa vào `node_call_llm` |
| ④ | Không khẳng định khi thiếu bằng chứng | 4 chốt trong `transfer_payment` (Sơ đồ 3) |

---

## 3 · 4 lớp phòng thủ (Guardrails)

Đọc từ trên xuống — mỗi lớp có lối thoát riêng, hỏng lớp trên vẫn còn lớp dưới.
**Lớp 4 là Python thuần**, prompt injection không vượt qua được.

```mermaid
flowchart TB
    REQ(["Yêu cầu người dùng"])

    L1{"LỚP 1 · Kiểm duyệt đầu vào<br/>luật từ khoá tiếng Việt<br/>+ LLM phân loại"}
    S1["MODERATION_BLOCKED<br/>0 tool · 0 vòng lặp"]

    L2{"LỚP 2 · Phân quyền theo vai<br/>roles.get_allowed_tools<br/>tool bị cấm KHÔNG có trong prompt"}
    S2["Từ chối vượt quyền<br/>+ ghi audit_log"]

    L3{"LỚP 3 · Phanh vòng lặp<br/>lặp hành động · hết budget<br/>· provider trả lỗi"}
    S3["REPEATED_ACTION<br/>MAX_ITERATIONS<br/>PROVIDER_ERROR"]

    L4{"LỚP 4 · Chốt trong tool<br/>là hành động tiêu tiền<br/>và ≥ 10.000.000đ?"}
    HUMAN{"node_human_approval<br/>CLI hỏi y/N · Streamlit nút Duyệt<br/>cờ duyệt nằm NGOÀI tham số tool"}
    S4["HUMAN_REJECTED<br/>KHÔNG đồng nào được chuyển"]

    GATES{"4 chốt trong transfer_payment<br/>① đã thanh toán chưa<br/>② mã số thuế hợp lệ chưa<br/>③ vượt hạn mức hạng mục không<br/>④ đã có phê duyệt của người chưa"}
    S5["LỖI CHÍNH SÁCH<br/>+ ghi audit_log"]
    OK(["Thực thi thật"])

    REQ --> L1
    L1 -- "INJECTION / ABUSE" --> S1
    L1 -- "SAFE" --> L2
    L2 -- "vai không có quyền" --> S2
    L2 -- "có quyền" --> L3
    L3 -- "chạm phanh" --> S3
    L3 -- "còn an toàn" --> L4
    L4 -- "có" --> HUMAN
    HUMAN -- "TỪ CHỐI" --> S4
    HUMAN -- "ĐỒNG Ý → grant_approval" --> GATES
    L4 -- "không · chỉ đọc<br/>hoặc số tiền nhỏ" --> GATES
    GATES -- "vi phạm bất kỳ chốt nào" --> S5
    GATES -- "qua hết 4 chốt" --> OK

    classDef guard fill:#F5E6E3,stroke:#B23A32,stroke-width:2px,color:#8E2C25
    classDef stop fill:#FBEFED,stroke:#B23A32,color:#8E2C25
    classDef terminal fill:#171A1F,stroke:#171A1F,color:#FFFFFF

    class L1,L2,L3,L4,HUMAN,GATES guard
    class S1,S2,S3,S4,S5 stop
    class REQ,OK terminal
```

Mọi nhánh dừng — kể cả hành động **bị chặn** — đều ghi vào bảng `audit_log`.
Xem bằng `python src/app.py --audit-blocked`.

---

## 4 · Tầng công cụ & tầng dữ liệu

11 tool trong `src/tools.py`, lọc theo vai **trước khi** đưa vào prompt:
kế toán thấy 11 tool, nhân viên chỉ thấy 7.

```mermaid
flowchart LR
    EXEC(["execute_tool"])

    subgraph T_READ["🔍 Chỉ đọc — 6 tool"]
        A1["list_invoice_files"]
        A2["query_finance_db"]
        A3["get_my_invoice_status"]
        A4["generate_business_report"]
        A5["check_policy_compliance"]
        A6["check_duplicate_invoice"]
    end

    subgraph T_KNOW["📚 Tri thức ngoài — 2 tool"]
        B1["ocr_invoice<br/>GPT-4o Vision<br/>thiếu trường thì ĐỂ TRỐNG"]
        B2["search_policy<br/>BM25 tự viết<br/>trả kèm trích dẫn nguồn"]
    end

    subgraph T_WRITE["✍️ Ghi dữ liệu — 3 tool"]
        C1["create_payment_ticket"]
        C2["approve_ticket"]
        C3["💸 transfer_payment<br/>qua bank_api giả lập"]
    end

    DB[("SQLite · expenses.db<br/>invoices · payments · tickets")]
    AUD[("audit_log<br/>ghi CẢ hành động bị chặn")]
    POL[/"data/policies/*.md<br/>4 văn bản · 33 điều"/]
    IMG[/"data/invoices/*.jpg<br/>PII · KHÔNG commit"/]

    EXEC --> T_READ
    EXEC --> T_KNOW
    EXEC --> T_WRITE

    T_READ --> DB
    T_WRITE --> DB
    T_WRITE --> AUD
    B2 --> POL
    B1 --> IMG

    classDef tool fill:#FBFAF8,stroke:#8A9099,color:#171A1F
    classDef store fill:#F6EDDC,stroke:#9A6B1F,color:#6B4A14
    classDef terminal fill:#171A1F,stroke:#171A1F,color:#FFFFFF

    class A1,A2,A3,A4,A5,A6,B1,B2,C1,C2,C3 tool
    class DB,AUD,POL,IMG store
    class EXEC terminal
```

| Vai | Số tool | Không được thấy |
| :--- | :-: | :--- |
| `ke_toan` — kế toán | 11 | — |
| `nhan_vien` — nhân viên | 7 | `transfer_payment`, `approve_ticket`, `query_finance_db`, `generate_business_report` |

---

## 5 · Ánh xạ sang rubric chấm điểm

README §4 có 5 tiêu chí. Bảng dưới trỏ thẳng tới **bằng chứng** của từng tiêu chí.

| # | Tiêu chí | % | Bằng chứng trong repo |
| :-: | :--- | :-: | :--- |
| ① | Agentic Fit & Test Design | 20% | `brainstorm.md` (Scoring Matrix 19/20) · `config/test_cases.json` — 12 case: 2 đơn giản, 2 một-tool, 4 multi-step, 4 bẫy/tấn công |
| ② | ReAct Implementation & Tools | 30% | `src/graph.py` — `parse_llm_output`, `parse_action`, `execute_tool` viết tay (**Sơ đồ 2**) · `src/tools.py` — `TOOL_SPECS` phủ đủ 8 câu hỏi tool contract (**Sơ đồ 4**) |
| ③ | Guardrails & Observability | 20% | Lớp 3 phanh vòng lặp (**Sơ đồ 3**) · bảng `audit_log` · `docs/trace_run_log.md` sinh bởi `--save-trace` |
| ④ | Inter-group Attack & Defense | 20% | Lớp 1, 2, 4 (**Sơ đồ 3**) · TC#7 prompt injection · TC#8 vượt quyền · TC#9 thanh toán trùng · TC#12 cô lập HITL |
| ⑤ | Hybrid Decision Flowchart | 10% | **Sơ đồ 1** (vẽ tay) · `docs/hybrid_flowchart_mermaid.md` (sinh tự động từ code) |

Sinh lại sơ đồ tự động sau khi sửa graph:

```bash
python src/app.py --export-flowchart
```
