"""
🖥️ GIAO DIỆN WEB — HAI INTERFACE THEO VAI TRÒ

  👔 Kế toán   : 5 tab — Xử lý hóa đơn · Hàng đợi Ticket · Báo cáo · Nhật ký Audit · Chat
  👤 Nhân viên : 3 tab — Nộp hóa đơn · Hóa đơn của tôi · Chat

Chạy:  streamlit run src/ui_streamlit.py

⚠️ GHI CHÚ KỸ THUẬT — Human-in-the-loop trong Streamlit:
   Streamlit chạy lại toàn bộ script mỗi lần tương tác, nên KHÔNG thể "dừng chờ"
   người bấm nút bên trong một callback đồng bộ. Giải pháp dùng ở đây là mẫu 2 pha:

     Pha 1 — Agent chạy, gặp giao dịch cần duyệt. Callback tra `st.session_state`
             không thấy phê duyệt nào ➔ TỪ CHỐI (an toàn) và ghi lại giao dịch đang chờ.
     Pha 2 — UI hiện thẻ xác nhận. Người dùng bấm "Duyệt" ➔ ghi cờ vào session_state
             ➔ chạy lại Agent với đúng câu hỏi cũ. Lần này callback thấy cờ ➔ CHO PHÉP.

   Mặc định khi thiếu thông tin luôn là TỪ CHỐI (fail-safe), không bao giờ fail-open.
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import streamlit as st

import bank_api
import database as db
import graph as agent_graph
import ocr_client
import prompts
import roles
import tools as tool_module
from providers import get_llm_provider

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

st.set_page_config(page_title="Trợ Lý Duyệt Chi Phí Doanh Nghiệp",
                   page_icon="🧾", layout="wide")


# =============================================================================
# KHỞI TẠO
# =============================================================================

@st.cache_resource
def init_app():
    """Khởi tạo DB và provider một lần duy nhất cho cả phiên."""
    db.init_db()
    return get_llm_provider()


def init_state():
    defaults = {
        "approvals": {},        # {approval_key: True} — các giao dịch đã được người duyệt
        "pending": None,        # giao dịch đang chờ xác nhận
        "pending_query": "",    # câu hỏi sinh ra giao dịch đó, để chạy lại pha 2
        "pending_role": "",
        "pending_user": "",
        "last_trace": None,
        "chat_history": [],
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


provider = init_app()
init_state()


# =============================================================================
# CẦU NỐI HUMAN-IN-THE-LOOP
# =============================================================================

def streamlit_approval_callback(pending: dict) -> bool:
    """
    Callback được graph gọi khi gặp giao dịch cần người duyệt.

    Chỉ trả True khi đã có cờ phê duyệt trong session_state (do người dùng bấm nút
    ở lần chạy trước). Mặc định TỪ CHỐI.
    """
    key = tool_module.approval_key(pending.get("invoice_no", ""), pending.get("amount", 0))
    if st.session_state["approvals"].get(key):
        return True

    st.session_state["pending"] = dict(pending)
    return False


def run_agent_ui(query: str, role: str, user_id: str):
    """Chạy Agent và lưu trace vào session_state."""
    trace = agent_graph.run_agent(
        user_query=query, role=role, user_id=user_id, provider=provider,
        approval_callback=streamlit_approval_callback, verbose=False,
    )
    st.session_state["last_trace"] = trace
    st.session_state["pending_query"] = query
    st.session_state["pending_role"] = role
    st.session_state["pending_user"] = user_id
    return trace


# =============================================================================
# THÀNH PHẦN GIAO DIỆN DÙNG CHUNG
# =============================================================================

def render_trace(trace: dict):
    """Hiển thị chuỗi Thought → Action → Observation."""
    if not trace:
        return

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Tuyến", trace.get("route") or "REFUSE")
    c2.metric("Số lần gọi tool", trace.get("tool_calls", 0))
    c3.metric("Số vòng lặp", trace.get("iterations", 0))
    c4.metric("Lý do dừng", trace.get("stop_reason", ""))

    steps = trace.get("steps", [])
    if not steps:
        st.info("Không có bước ReAct nào — câu hỏi đi thẳng đường Chatbot hoặc bị chặn ở kiểm duyệt.")
        return

    st.markdown("##### 🔍 Nhật ký suy luận")
    for s in steps:
        label = s.get("tool") or ("Final Answer" if s.get("final_answer") else "Bước")
        with st.expander(f"Bước {s.get('step')} — {label}",
                         expanded=(s is steps[-1])):
            if s.get("thought"):
                st.markdown(f"🧠 **Thought:** {s['thought']}")
            if s.get("tool"):
                args = ", ".join(f'"{a}"' for a in s.get("args", []))
                st.code(f"Action: {s['tool']}[{args}]", language="text")
            if s.get("observation"):
                st.markdown("👁️ **Observation:**")
                st.code(s["observation"], language="text")
            if s.get("final_answer"):
                st.success(s["final_answer"])


def render_pending_approval():
    """Thẻ xác nhận giao dịch — hiện thực pha 2 của human-in-the-loop."""
    pending = st.session_state.get("pending")
    if not pending:
        return False

    st.warning("🖐️ **GIAO DỊCH CẦN XÁC NHẬN CỦA NGƯỜI CÓ THẨM QUYỀN**")
    st.markdown(
        f"Số tiền đạt/vượt ngưỡng **{db.format_money(tool_module.HIGH_VALUE_THRESHOLD)}** "
        f"nên theo Điều 6 QC-TC-01/2026 (nguyên tắc bốn mắt), hệ thống tự động "
        f"**không được phép** tự thực hiện."
    )

    col1, col2 = st.columns(2)
    with col1:
        st.markdown(f"""
| Trường | Giá trị |
| :--- | :--- |
| Hóa đơn | `{pending.get('invoice_no')}` |
| Nhà cung cấp | {pending.get('vendor')} |
| Số tiền | **{db.format_money(pending.get('amount', 0))}** |
| Hạng mục | {pending.get('category')} |
| Người yêu cầu | {pending.get('user_id')} |
""")

    with col2:
        st.write("")
        if st.button("✅ DUYỆT giao dịch", type="primary", use_container_width=True):
            key = tool_module.approval_key(pending.get("invoice_no", ""),
                                           pending.get("amount", 0))
            st.session_state["approvals"][key] = True
            st.session_state["pending"] = None
            run_agent_ui(st.session_state["pending_query"],
                         st.session_state["pending_role"],
                         st.session_state["pending_user"])
            st.rerun()

        if st.button("❌ TỪ CHỐI", use_container_width=True):
            db.write_audit(tool="streamlit_approval",
                           role=st.session_state["pending_role"],
                           user_id=st.session_state["pending_user"],
                           args=str(pending.get("invoice_no")),
                           result="REJECTED", blocked_reason="Người duyệt từ chối trên UI")
            st.session_state["pending"] = None
            st.error("Đã từ chối. Không có khoản tiền nào được chuyển đi.")
            st.rerun()

    return True


def invoice_picker(label: str = "Chọn ảnh hóa đơn"):
    """Dropdown chọn ảnh trong data/invoices/."""
    images = ocr_client.list_invoice_images()
    if not images:
        st.warning("Thư mục `data/invoices/` đang trống. "
                   "Hãy chép ảnh hóa đơn vào đó rồi tải lại trang.")
        return ""
    return st.selectbox(label, images)


# =============================================================================
# SIDEBAR — ĐĂNG NHẬP & TRẠNG THÁI HỆ THỐNG
# =============================================================================

with st.sidebar:
    st.title("🧾 Trợ Lý Duyệt Chi Phí")
    st.caption("VinUni Lab 03 — Chatbot vs ReAct Agent")

    st.divider()
    st.subheader("👤 Đăng nhập")

    role_label = st.radio("Vai trò", ["👔 Kế toán", "👤 Nhân viên"], index=0)
    role = roles.KE_TOAN if "Kế toán" in role_label else roles.NHAN_VIEN

    users = roles.get_users_by_role(role)
    user_id = st.selectbox(
        "Tài khoản", list(users),
        format_func=lambda uid: f"{uid} — {users[uid]['name']} ({users[uid]['department']})",
    )

    st.info(roles.ROLES[role]["description"])

    allowed = sorted(roles.get_allowed_tools(role))
    hidden = sorted(set(roles.ROLE_TOOL_PERMISSIONS[roles.KE_TOAN]) - set(allowed))
    with st.expander(f"🔐 Quyền hạn ({len(allowed)} công cụ)"):
        for t in allowed:
            st.markdown(f"- ✅ `{t}`")
        for t in hidden:
            st.markdown(f"- 🔒 ~~`{t}`~~ (không có quyền)")

    st.divider()
    st.subheader("⚙️ Trạng thái hệ thống")

    st.markdown(f"**LLM:** `{provider.__class__.__name__}`")
    st.caption(f"model: {getattr(provider, 'model_name', 'mock')}")
    if provider.__class__.__name__ == "MockProvider":
        st.error("Đang dùng MockProvider — hãy điền GEMINI_API_KEY vào .env")

    ocr_state = ocr_client.check_ocr_connection()
    st.markdown(f"**OCR:** {'🟢 kết nối được' if ocr_state['ok'] else '🔴 không kết nối được'}")
    st.caption(ocr_client.get_config()["base_url"])

    st.markdown(f"**Số dư công ty:** {bank_api.format_money(bank_api.get_balance())}")

    with st.expander("🛡️ Guardrails"):
        st.markdown(f"""
- `MAX_ITERATIONS` = **{prompts.MAX_ITERATIONS}**
- `MAX_REPEATED_ACTIONS` = **{prompts.MAX_REPEATED_ACTIONS}**
- Ngưỡng duyệt tay = **{db.format_money(tool_module.HIGH_VALUE_THRESHOLD)}**
""")

st.title(f"{role_label} — Trợ Lý Duyệt Chi Phí Doanh Nghiệp")


# =============================================================================
# 👔 GIAO DIỆN KẾ TOÁN
# =============================================================================

if role == roles.KE_TOAN:
    tab1, tab2, tab3, tab4, tab5 = st.tabs(
        ["📷 Xử lý hóa đơn", "🎫 Hàng đợi Ticket", "📊 Báo cáo",
         "🔍 Nhật ký Audit", "💬 Chat với Agent"]
    )

    # ---------------------------------------------------------------- Tab 1
    with tab1:
        st.subheader("Xử lý và thanh toán hóa đơn")

        if render_pending_approval():
            st.divider()

        image = invoice_picker()
        col1, col2 = st.columns([3, 1])
        with col1:
            custom = st.text_input(
                "Yêu cầu gửi Agent",
                value=f"Xử lý và thanh toán hóa đơn data/invoices/{image}" if image else "",
            )
        with col2:
            st.write("")
            st.write("")
            run = st.button("🚀 Chạy Agent", type="primary", use_container_width=True)

        if run and custom.strip():
            with st.spinner("Agent đang suy luận..."):
                trace = run_agent_ui(custom, role, user_id)
            st.divider()
            if trace["stop_reason"] == "HUMAN_REJECTED" and st.session_state.get("pending"):
                st.rerun()
            st.markdown("#### 🏁 Kết quả")
            st.info(trace["final_answer"])
            render_trace(trace)
        elif st.session_state.get("last_trace"):
            st.divider()
            st.caption("Kết quả lần chạy gần nhất:")
            st.info(st.session_state["last_trace"]["final_answer"])
            render_trace(st.session_state["last_trace"])

    # ---------------------------------------------------------------- Tab 2
    with tab2:
        st.subheader("Hàng đợi phiếu đề nghị duyệt chi")

        status_filter = st.radio("Lọc theo trạng thái",
                                 ["OPEN", "APPROVED", "REJECTED", "Tất cả"],
                                 horizontal=True)
        tickets = db.list_tickets(status="" if status_filter == "Tất cả" else status_filter)

        if not tickets:
            st.info("Không có phiếu đề nghị nào khớp bộ lọc.")
        for t in tickets:
            icon = {"OPEN": "🟡", "APPROVED": "🟢", "REJECTED": "🔴"}.get(t["status"], "⚪")
            with st.expander(
                f"{icon} Phiếu #{t['id']} — {t['invoice_no'] or '(không gắn HĐ)'} — "
                f"{db.format_money(t['amount'] or 0)} — {t['status']}"
            ):
                st.markdown(f"""
- **Nhà cung cấp**: {t['vendor'] or '(không rõ)'}
- **Người đề nghị**: {t['requested_by']}
- **Gửi tới**: {t['target_department']}
- **Lý do**: {t['reason']}
- **Tạo lúc**: {t['created_at']}
""")
                if t["status"] == db.TICKET_OPEN:
                    note = st.text_input("Ghi chú xử lý", key=f"note_{t['id']}")
                    c1, c2 = st.columns(2)
                    if c1.button("✅ Phê duyệt", key=f"ap_{t['id']}", use_container_width=True):
                        tool_module.set_session(role, user_id)
                        st.success(tool_module.approve_ticket(t["id"], "APPROVE", note))
                        st.rerun()
                    if c2.button("❌ Từ chối", key=f"rj_{t['id']}", use_container_width=True):
                        tool_module.set_session(role, user_id)
                        st.error(tool_module.approve_ticket(t["id"], "REJECT", note))
                        st.rerun()
                else:
                    st.caption(f"Xử lý bởi {t['resolved_by'] or '-'} lúc {t['resolved_at'] or '-'}"
                               + (f" — {t['resolution_note']}" if t["resolution_note"] else ""))

    # ---------------------------------------------------------------- Tab 3
    with tab3:
        st.subheader("Báo cáo tài chính")

        c1, c2, c3 = st.columns([1, 1, 1])
        period_type = c1.selectbox("Kỳ báo cáo", ["quarter", "month", "week"],
                                   format_func=lambda x: {"week": "Theo tuần",
                                                          "month": "Theo tháng",
                                                          "quarter": "Theo quý"}[x])
        defaults = {"quarter": "2026-Q2", "month": "2026-07", "week": "2026-W28"}
        period_value = c2.text_input("Giá trị kỳ", value=defaults[period_type])
        c3.write("")
        c3.write("")
        go = c3.button("📊 Xuất báo cáo", type="primary", use_container_width=True)

        if go or period_value:
            rep = db.financial_report(period_type, period_value)
            if not rep["ok"]:
                st.error(rep["error"])
            elif rep["count"] == 0:
                st.warning(f"Không có giao dịch nào trong {rep['period_label']}.")
            else:
                m1, m2, m3 = st.columns(3)
                m1.metric("Tổng chi phí", db.format_money(rep["total"]))
                m2.metric("Số hóa đơn", rep["count"])
                m3.metric("Trung bình/HĐ", db.format_money(rep["total"] / rep["count"]))

                st.markdown(f"##### Phân bổ theo hạng mục — {rep['period_label']}")
                chart = {c["category"]: c["total"] for c in rep["by_category"]}
                st.bar_chart(chart)

                st.dataframe(
                    [{"Hạng mục": c["category"],
                      "Số tiền": db.format_money(c["total"]),
                      "Số HĐ": c["cnt"],
                      "Tỉ trọng": f"{c['total'] / rep['total'] * 100:.1f}%"}
                     for c in rep["by_category"]],
                    use_container_width=True, hide_index=True,
                )

                st.markdown("##### Top nhà cung cấp")
                st.dataframe(
                    [{"Nhà cung cấp": v["vendor"],
                      "Số tiền": db.format_money(v["total"]),
                      "Số HĐ": v["cnt"]} for v in rep["top_vendors"]],
                    use_container_width=True, hide_index=True,
                )

    # ---------------------------------------------------------------- Tab 4
    with tab4:
        st.subheader("Nhật ký kiểm toán")
        st.caption("Điều 7 QT-TC-03/2026 — ghi lại MỌI hành động ghi dữ liệu, "
                   "kể cả những hành động bị hệ thống TỪ CHỐI.")

        only_blocked = st.checkbox("🚫 Chỉ hiện các hành động BỊ CHẶN")
        rows = db.list_audit(limit=100, only_blocked=only_blocked)

        if not rows:
            st.info("Chưa có bản ghi nào.")
        else:
            blocked_count = sum(1 for r in rows if r["blocked_reason"])
            c1, c2 = st.columns(2)
            c1.metric("Tổng bản ghi hiển thị", len(rows))
            c2.metric("Trong đó bị chặn", blocked_count)

            st.dataframe(
                [{"Thời điểm": r["ts"],
                  "": "🚫" if r["blocked_reason"] else "✅",
                  "Vai": r["role"] or "-",
                  "Người dùng": r["user_id"] or "-",
                  "Công cụ": r["tool"],
                  "Tham số": str(r["args"] or "")[:60],
                  "Lý do chặn": (r["blocked_reason"] or "")[:80]} for r in rows],
                use_container_width=True, hide_index=True,
            )

    # ---------------------------------------------------------------- Tab 5
    with tab5:
        st.subheader("Hỏi Agent bất cứ điều gì")
        st.caption("Ví dụ: *Hạn mức tiếp khách là bao nhiêu?* · "
                   "*Hóa đơn nào đang PENDING?* · *Báo cáo quý 2/2026*")

        if render_pending_approval():
            st.divider()

        query = st.text_input("Câu hỏi", key="chat_ketoan")
        if st.button("Gửi", type="primary", key="send_ketoan") and query.strip():
            with st.spinner("Agent đang suy luận..."):
                trace = run_agent_ui(query, role, user_id)
            if st.session_state.get("pending"):
                st.rerun()
            st.info(trace["final_answer"])
            render_trace(trace)


# =============================================================================
# 👤 GIAO DIỆN NHÂN VIÊN
# =============================================================================

else:
    tab1, tab2, tab3 = st.tabs(["📤 Nộp hóa đơn", "📋 Hóa đơn của tôi", "💬 Chat với Agent"])

    # ---------------------------------------------------------------- Tab 1
    with tab1:
        st.subheader("Nộp hóa đơn để đề nghị thanh toán")
        st.caption("Agent sẽ nhận dạng hóa đơn, đối chiếu chính sách nội bộ, rồi lập "
                   "phiếu đề nghị gửi Phòng Tài chính - Kế toán xử lý.")

        image = invoice_picker()
        default_q = (f"Kiểm tra hóa đơn data/invoices/{image} có hợp lệ không, "
                     f"nếu hợp lệ thì lập phiếu đề nghị thanh toán giúp tôi." if image else "")
        custom = st.text_input("Yêu cầu gửi Agent", value=default_q)

        if st.button("🚀 Gửi cho Agent", type="primary") and custom.strip():
            with st.spinner("Agent đang xử lý..."):
                trace = run_agent_ui(custom, role, user_id)
            st.divider()
            st.markdown("#### 🏁 Kết quả")
            st.info(trace["final_answer"])
            render_trace(trace)

    # ---------------------------------------------------------------- Tab 2
    with tab2:
        st.subheader("Hóa đơn tôi đã nộp")
        rows = db.list_invoices_by_user(user_id)

        if not rows:
            st.info("Bạn chưa nộp hóa đơn nào.")
        else:
            counts = {}
            for r in rows:
                counts[r["status"]] = counts.get(r["status"], 0) + 1
            cols = st.columns(max(len(counts), 1))
            for col, (status, n) in zip(cols, sorted(counts.items())):
                col.metric(status, n)

            st.dataframe(
                [{"Số HĐ": r["invoice_no"], "Nhà cung cấp": r["vendor"],
                  "Hạng mục": r["category"], "Số tiền": db.format_money(r["amount"]),
                  "Ngày": r["invoice_date"], "Trạng thái": r["status"]} for r in rows],
                use_container_width=True, hide_index=True,
            )

            st.markdown("##### Phiếu đề nghị của tôi")
            my_tickets = db.list_tickets(requested_by=user_id)
            if my_tickets:
                st.dataframe(
                    [{"Phiếu": f"#{t['id']}", "Hóa đơn": t["invoice_no"],
                      "Số tiền": db.format_money(t["amount"] or 0),
                      "Gửi tới": t["target_department"], "Trạng thái": t["status"]}
                     for t in my_tickets],
                    use_container_width=True, hide_index=True,
                )
            else:
                st.caption("Chưa có phiếu đề nghị nào.")

    # ---------------------------------------------------------------- Tab 3
    with tab3:
        st.subheader("Hỏi Agent về chính sách chi phí")
        st.caption("Ví dụ: *Hạn mức tiếp khách là bao nhiêu?* · "
                   "*Hóa đơn của tôi đến đâu rồi?* · *Đi công tác cần giữ giấy tờ gì?*")

        query = st.text_input("Câu hỏi", key="chat_nv")
        if st.button("Gửi", type="primary", key="send_nv") and query.strip():
            with st.spinner("Agent đang suy luận..."):
                trace = run_agent_ui(query, role, user_id)
            st.info(trace["final_answer"])
            render_trace(trace)

        st.divider()
        st.caption("🔒 Lưu ý: vai trò Nhân viên không có quyền chuyển khoản, "
                   "duyệt phiếu, truy vấn sổ cái toàn công ty hay xuất báo cáo. "
                   "Nếu bạn yêu cầu những việc đó, Agent sẽ từ chối và đề xuất "
                   "lập phiếu đề nghị.")
