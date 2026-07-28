"""
🚀 CORE APP — CLI CHẠY CHATBOT BASELINE & REACT AGENT
(Role 4: Core Developer / Integrator)

Ghép nối toàn bộ các mảnh: tools.py + prompts.py + graph.py + providers.py
+ config/test_cases.json thành một ứng dụng chạy được.

CÁCH DÙNG NHANH
───────────────
  python src/app.py                          # chạy demo mặc định (test case #4)
  python src/app.py --all --mode both        # chạy cả 9 test case, cả 2 chế độ
  python src/app.py --case 5                 # chạy riêng test case #5
  python src/app.py --query "..." --role ketoan --user KT-01
  python src/app.py --interactive            # chat trực tiếp với Agent
  python src/app.py --save-trace docs/trace_run_log.md

TIỆN ÍCH
────────
  python src/app.py --init-db                # tạo/seed database
  python src/app.py --reset-db               # xoá và seed lại từ đầu
  python src/app.py --check-ocr              # kiểm tra kết nối service OCR
  python src/app.py --report month 2026-07   # xuất báo cáo nhanh
  python src/app.py --export-flowchart       # sinh docs/hybrid_flowchart.mermaid
  python src/app.py --audit                  # xem nhật ký kiểm toán
"""

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from dotenv import load_dotenv

import bank_api
import database as db
import graph as agent_graph
import ocr_client
import prompts
import roles
import tools as tool_module
from providers import get_llm_provider

load_dotenv()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config", "test_cases.json")
FLOWCHART_PATH = os.path.join(BASE_DIR, "docs", "hybrid_flowchart.mermaid")


# =============================================================================
# TIỆN ÍCH HIỂN THỊ
# =============================================================================

def banner(title: str, char: str = "=", width: int = 78):
    print("\n" + char * width)
    print(title)
    print(char * width)


def load_test_cases() -> list:
    """Đọc bộ test case của Role 1."""
    path = CONFIG_PATH if os.path.exists(CONFIG_PATH) else "test_cases.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# =============================================================================
# CHATBOT BASELINE (Cấp 2 — đúng 1 LLM call, KHÔNG tool)
# =============================================================================

def run_baseline_chatbot(user_query: str, provider, verbose: bool = True) -> dict:
    """
    Đường cơ sở để so sánh: một lần gọi LLM duy nhất, không có công cụ nào.

    Returns:
        dict: {question, answer, tool_calls, iterations, stop_reason, mode}
    """
    if verbose:
        banner(f"💬 CHATBOT BASELINE (Cấp 2 — LLM thuần)", "─")
        print(f"❓ Câu hỏi: {user_query}")

    answer = provider.generate(user_query, system_prompt=prompts.CHATBOT_BASELINE_PROMPT)

    if verbose:
        print(f"\n🤖 Trả lời:\n{answer}")
        print(f"\n📊 Số lần gọi tool: 0  |  Số lần gọi LLM: 1")

    return {
        "mode": "chatbot",
        "question": user_query,
        "answer": answer,
        "tool_calls": 0,
        "iterations": 1,
        "stop_reason": "FINAL_ANSWER",
    }


# =============================================================================
# HUMAN-IN-THE-LOOP TRÊN CLI
# =============================================================================

def cli_approval_callback(pending: dict) -> bool:
    """Hỏi người dùng y/N trên terminal trước khi cho phép chuyển tiền."""
    print("\n" + "!" * 78)
    print("🖐️  YÊU CẦU XÁC NHẬN CHUYỂN KHOẢN")
    print("!" * 78)
    print(f"   Hóa đơn      : {pending.get('invoice_no')}")
    print(f"   Nhà cung cấp : {pending.get('vendor')}")
    print(f"   Số tiền      : {db.format_money(pending.get('amount', 0))}")
    print(f"   Hạng mục     : {pending.get('category')}")
    print(f"   Người yêu cầu: {pending.get('user_id')} ({pending.get('role')})")
    print("!" * 78)

    try:
        answer = input("   ➤ Bạn DUYỆT giao dịch này? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n   (không nhận được phản hồi → mặc định TỪ CHỐI)")
        return False
    return answer in ("y", "yes", "d", "duyet", "duyệt")


def auto_reject_callback(pending: dict) -> bool:
    """Dùng khi chạy tự động (--all): luôn TỪ CHỐI để không tiêu tiền ngoài ý muốn."""
    print("   🤖 [CHẾ ĐỘ TỰ ĐỘNG] Mặc định TỪ CHỐI giao dịch cần duyệt tay "
          "(dùng --interactive để duyệt thủ công)")
    return False


# =============================================================================
# CHẠY REACT AGENT
# =============================================================================

def run_react_agent(user_query: str, provider, role: str = roles.KE_TOAN,
                    user_id: str = "", approval_callback=None,
                    verbose: bool = True) -> dict:
    """Chạy Agent qua LangGraph 6 node. Trả về trace có cấu trúc."""
    if verbose:
        banner(f"🤖 REACT AGENT (Cấp 3 — LangGraph 6 node)", "─")
        print(f"❓ Câu hỏi: {user_query}")
        print(f"👤 Vai trò: {roles.ROLES[roles.normalize_role(role)]['label']} ({user_id})")

    trace = agent_graph.run_agent(
        user_query=user_query, role=role, user_id=user_id, provider=provider,
        approval_callback=approval_callback or auto_reject_callback, verbose=verbose,
    )
    trace["mode"] = "agent"

    if verbose:
        print(f"\n{'─' * 66}")
        print(f"🏁 KẾT QUẢ CUỐI CÙNG:\n{trace['final_answer']}")
        print(f"\n📊 Tuyến: {trace['route'] or 'REFUSE'}  |  "
              f"Số vòng lặp: {trace['iterations']}  |  "
              f"Số lần gọi tool: {trace['tool_calls']}  |  "
              f"Lý do dừng: {trace['stop_reason']}")

    return trace


# =============================================================================
# XUẤT TRACE RA MARKDOWN (cho Role 5 dán vào docs/trace_eval.md)
# =============================================================================

def format_trace_markdown(trace: dict, test_case: dict = None) -> str:
    """Kết xuất một lượt chạy Agent thành markdown dán thẳng vào báo cáo."""
    lines = []
    title = f"Test Case #{test_case['id']}" if test_case else "Truy vấn tự do"
    lines.append(f"### {title}")
    lines.append("")

    if test_case:
        lines.append(f"**Loại**: {test_case.get('category', '')}")
    lines.append(f"**Câu hỏi**: *\"{trace['question']}\"*")
    lines.append(f"**Vai trò**: {trace.get('role', '')} / {trace.get('user_id', '') or '(ẩn danh)'}")
    lines.append("")
    lines.append(f"| Chỉ số | Giá trị |")
    lines.append(f"| :--- | :--- |")
    lines.append(f"| Tuyến định tuyến | `{trace.get('route') or 'REFUSE'}` |")
    lines.append(f"| Kiểm duyệt | `{trace.get('moderation_label') or 'n/a'}` |")
    lines.append(f"| Số vòng lặp | {trace.get('iterations', 0)} |")
    lines.append(f"| Số lần gọi tool | {trace.get('tool_calls', 0)} |")
    lines.append(f"| Lý do dừng | `{trace.get('stop_reason', '')}` |")
    lines.append("")

    steps = trace.get("steps", [])
    if steps:
        lines.append("**Chuỗi Thought → Action → Observation:**")
        lines.append("")
        lines.append("```text")
        lines.append(f"Question: {trace['question']}")
        lines.append("")
        for s in steps:
            if s.get("thought"):
                lines.append(f"Thought {s['step']}: {s['thought']}")
            if s.get("tool"):
                args = ", ".join(f'"{a}"' for a in s.get("args", []))
                lines.append(f"Action {s['step']}: {s['tool']}[{args}]")
            if s.get("observation"):
                obs = str(s["observation"])
                if len(obs) > 600:
                    obs = obs[:600] + "\n... (đã rút gọn)"
                lines.append(f"Observation {s['step']}: {obs}")
            if s.get("final_answer"):
                lines.append(f"Final Answer: {s['final_answer']}")
            lines.append("")
        lines.append("```")
        lines.append("")

    lines.append("**Câu trả lời cuối cùng:**")
    lines.append("")
    lines.append("> " + str(trace.get("final_answer", "")).replace("\n", "\n> "))
    lines.append("")
    return "\n".join(lines)


def format_comparison_markdown(chatbot: dict, agent: dict, test_case: dict = None) -> str:
    """Bảng so sánh Chatbot vs Agent cho cùng một câu hỏi."""
    lines = ["#### So sánh Chatbot Baseline vs ReAct Agent", "",
             "| | 🤖 Chatbot Baseline (Cấp 2) | 🧠 ReAct Agent (Cấp 3) |",
             "| :--- | :--- | :--- |",
             f"| Số lần gọi tool | {chatbot['tool_calls']} | {agent['tool_calls']} |",
             f"| Số lần gọi LLM | {chatbot['iterations']} | {agent['iterations']} |",
             f"| Lý do dừng | `{chatbot['stop_reason']}` | `{agent['stop_reason']}` |",
             ""]

    def _quote(text, limit=700):
        text = str(text or "").strip()
        if len(text) > limit:
            text = text[:limit] + "\n... (đã rút gọn)"
        return "> " + text.replace("\n", "\n> ")

    lines.append("**Chatbot trả lời:**")
    lines.append("")
    lines.append(_quote(chatbot["answer"]))
    lines.append("")
    lines.append("**Agent trả lời:**")
    lines.append("")
    lines.append(_quote(agent["final_answer"]))
    lines.append("")
    return "\n".join(lines)


# =============================================================================
# CÁC LỆNH TIỆN ÍCH
# =============================================================================

def cmd_check_ocr():
    banner("📷 KIỂM TRA KẾT NỐI SERVICE OCR")
    cfg = ocr_client.get_config()
    print(f"   OCR_BASE_URL   : {cfg['base_url']}")
    print(f"   OCR_ENDPOINT   : {cfg['endpoint'] or '(trống — sẽ tự dò)'}")
    print(f"   OCR_FIELD_NAME : {cfg['field']}")
    print(f"   OCR_TIMEOUT    : {cfg['timeout']}s")

    result = ocr_client.check_ocr_connection()
    print(f"\n   {'✅' if result['ok'] else '❌'} {result['message']}")

    images = ocr_client.list_invoice_images()
    print(f"\n   📁 Ảnh trong data/invoices/: "
          f"{', '.join(images) if images else '(trống)'}")

    if result["ok"] and images:
        print(f"\n   🚀 Thử OCR file '{images[0]}'...")
        out = ocr_client.call_ocr(images[0])
        if out.get("ok"):
            for k in ("invoice_no", "vendor", "tax_code", "invoice_date", "amount"):
                print(f"      {k:<14}: {out.get(k)}")
            print(f"      thiếu         : {out.get('missing_fields') or 'không thiếu'}")
        else:
            print(f"      ❌ {out['error']}")


def cmd_report(period_type: str, period_value: str):
    banner(f"📊 BÁO CÁO {period_type.upper()} {period_value}")
    db.init_db()
    tool_module.set_session(roles.KE_TOAN, "KT-01")
    print(tool_module.generate_business_report(period_type, period_value))


def cmd_audit(limit: int = 20, only_blocked: bool = False):
    banner("🔍 NHẬT KÝ KIỂM TOÁN" + (" — CHỈ HÀNH ĐỘNG BỊ CHẶN" if only_blocked else ""))
    db.init_db()
    rows = db.list_audit(limit=limit, only_blocked=only_blocked)
    if not rows:
        print("   (chưa có bản ghi nào)")
        return
    for r in rows:
        flag = "🚫" if r["blocked_reason"] else "✅"
        print(f"\n   {flag} [{r['ts']}] {r['role'] or '-'}/{r['user_id'] or '-'} → {r['tool']}")
        if r["args"]:
            print(f"      Tham số : {str(r['args'])[:110]}")
        if r["blocked_reason"]:
            print(f"      BỊ CHẶN : {r['blocked_reason'][:110]}")
        elif r["result"]:
            print(f"      Kết quả : {str(r['result'])[:110]}")


def _split_frontmatter(mermaid: str):
    """
    Tách khối YAML front-matter (nếu có) ra khỏi phần thân sơ đồ.

    LangGraph sinh ra sơ đồ mở đầu bằng:
        ---
        config: ...
        ---
        graph TD;
    Mermaid BẮT BUỘC front-matter nằm ở ĐẦU FILE — chèn comment lên trước nó
    sẽ làm sơ đồ không render được. Vì vậy phải tách ra rồi ghép lại đúng thứ tự.

    Returns:
        (front_matter, body) — front_matter là chuỗi rỗng nếu không có.
    """
    text = (mermaid or "").lstrip("﻿").strip()
    if not text.startswith("---"):
        return "", text

    lines = text.splitlines()
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            return "\n".join(lines[:idx + 1]), "\n".join(lines[idx + 1:]).lstrip("\n")
    return "", text


def cmd_export_flowchart():
    banner("🗺️  XUẤT HYBRID FLOWCHART TỪ GRAPH ĐANG CHẠY")
    mermaid = agent_graph.export_mermaid()
    front_matter, body = _split_frontmatter(mermaid)

    # Lưu ý: KHÔNG được có dòng '%%' trống — mermaid coi đó là lỗi cú pháp.
    # Mọi dòng chú thích phải có nội dung (dù chỉ là dấu gạch) sau '%%'.
    header = f"""%% ============================================================================
%% HYBRID DECISION FLOWCHART — Trợ Lý Duyệt Chi Phí Doanh Nghiệp
%% ============================================================================
%% ⚠️ FILE NÀY ĐƯỢC SINH TỰ ĐỘNG — KHÔNG SỬA TAY.
%% Sinh lại bằng:  python src/app.py --export-flowchart
%% Nguồn: graph.get_graph().draw_mermaid() — tức sơ đồ này LUÔN khớp code thật.
%% Sinh lúc: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
%% -
%% Ý NGHĨA CÁC NHÁNH:
%%   moderation --refuse--> END           : Lớp 1 chặn prompt injection ngay cửa vào
%%   supervisor --direct--> direct_answer : 🤖 CHATBOT PATH (1 LLM call, 0 tool)
%%                                          Dùng cho câu hỏi kiến thức chung
%%   supervisor --react--> call_llm       : 🧠 REACT AGENT PATH (vòng lặp có tool)
%%                                          Dùng khi cần dữ liệu/hành động thật
%%   call_llm <-> tools                   : Vòng lặp Thought → Action → Observation
%%   tools --approval--> human_approval   : 🖐️ Chặn cứng trước khi chuyển tiền ≥ 10tr
%%   * --> exhausted                      : 🛑 Safe fallback khi chạm MAX_ITERATIONS
%% -
%% Xem thêm docs/flowchart.mermaid — bản vẽ tay đầy đủ, có tầng công cụ,
%% tầng dữ liệu, 4 lớp guardrails và ánh xạ sang rubric chấm điểm.
%% ============================================================================"""

    # Thứ tự bắt buộc: front-matter ➔ chú thích ➔ thân sơ đồ
    parts = [p for p in (front_matter, header, body) if p]
    content = "\n".join(parts) + "\n"

    os.makedirs(os.path.dirname(FLOWCHART_PATH), exist_ok=True)
    with open(FLOWCHART_PATH, "w", encoding="utf-8") as f:
        f.write(content)

    print(body)
    print(f"\n✅ Đã ghi vào {os.path.relpath(FLOWCHART_PATH, BASE_DIR)}")
    if front_matter:
        print("   (YAML front-matter được giữ nguyên ở đầu file để mermaid render được)")


def cmd_interactive(provider, role: str, user_id: str):
    banner(f"💬 CHẾ ĐỘ HỘI THOẠI — {roles.ROLES[roles.normalize_role(role)]['label']} ({user_id})")
    print("   Gõ câu hỏi và nhấn Enter. Gõ 'quit' hoặc Ctrl+C để thoát.")
    print("   Gõ 'switch' để đổi vai trò.\n")

    while True:
        try:
            query = input(f"\n[{user_id}] ➤ ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n👋 Tạm biệt!")
            return

        if not query:
            continue
        if query.lower() in ("quit", "exit", "thoat", "thoát"):
            print("👋 Tạm biệt!")
            return
        if query.lower() == "switch":
            role = roles.NHAN_VIEN if roles.normalize_role(role) == roles.KE_TOAN else roles.KE_TOAN
            user_id = "KT-01" if role == roles.KE_TOAN else "EMP-01"
            print(f"   🔄 Đã chuyển sang: {roles.ROLES[role]['label']} ({user_id})")
            continue

        run_react_agent(query, provider, role=role, user_id=user_id,
                        approval_callback=cli_approval_callback, verbose=True)


# =============================================================================
# CHẠY TEST CASES
# =============================================================================

def run_test_cases(cases: list, provider, mode: str, save_trace: str = "",
                   interactive_approval: bool = False) -> list:
    """Chạy danh sách test case ở chế độ chatbot / agent / both."""
    results = []
    callback = cli_approval_callback if interactive_approval else auto_reject_callback

    md_parts = [
        f"# 📋 NHẬT KÝ CHẠY TEST CASES",
        f"",
        f"*Sinh tự động bởi `python src/app.py` lúc "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*",
        f"",
        f"- Chế độ: `{mode}`",
        f"- Số test case: {len(cases)}",
        f"- LLM Provider: `{provider.__class__.__name__}` "
        f"(model: `{getattr(provider, 'model_name', 'n/a')}`)",
        f"- MAX_ITERATIONS: {prompts.MAX_ITERATIONS}",
        f"",
        "---",
        "",
    ]

    for case in cases:
        banner(f"🧪 TEST CASE #{case['id']} — {case['category']}")
        print(f"❓ {case['question']}")
        print(f"👤 Vai: {case.get('role', 'ke_toan')} / {case.get('user_id', '')}")
        print(f"🎯 Kỳ vọng: {case.get('expected_behavior', '')[:200]}")

        entry = {"case": case}
        md_parts.append(f"## Test Case #{case['id']} — {case['category']}")
        md_parts.append("")
        md_parts.append(f"**Kỳ vọng**: {case.get('expected_behavior', '')}")
        md_parts.append("")

        if mode in ("chatbot", "both"):
            entry["chatbot"] = run_baseline_chatbot(case["question"], provider)

        if mode in ("agent", "both"):
            entry["agent"] = run_react_agent(
                case["question"], provider,
                role=case.get("role", roles.KE_TOAN),
                user_id=case.get("user_id", ""),
                approval_callback=callback,
            )

        if mode == "both":
            md_parts.append(format_comparison_markdown(entry["chatbot"], entry["agent"], case))
            md_parts.append(format_trace_markdown(entry["agent"], case))
        elif mode == "agent":
            md_parts.append(format_trace_markdown(entry["agent"], case))
        else:
            md_parts.append("**Chatbot trả lời:**\n")
            md_parts.append("> " + str(entry["chatbot"]["answer"]).replace("\n", "\n> "))
            md_parts.append("")

        md_parts.append("---")
        md_parts.append("")
        results.append(entry)

    # ---------------- Bảng tổng kết ----------------
    banner("📈 TỔNG KẾT")
    header = f"{'#':<3} {'Loại':<38} {'Tuyến':<8} {'Tool':<5} {'Vòng':<5} {'Lý do dừng':<20}"
    print(header)
    print("─" * 84)

    summary_rows = ["| # | Loại | Tuyến | Tool | Vòng lặp | Lý do dừng |",
                    "| :-: | :--- | :--- | :-: | :-: | :--- |"]

    for entry in results:
        case = entry["case"]
        ag = entry.get("agent")
        if ag:
            print(f"{case['id']:<3} {case['category'][:36]:<38} "
                  f"{(ag['route'] or 'REFUSE'):<8} {ag['tool_calls']:<5} "
                  f"{ag['iterations']:<5} {ag['stop_reason']:<20}")
            summary_rows.append(
                f"| {case['id']} | {case['category']} | `{ag['route'] or 'REFUSE'}` | "
                f"{ag['tool_calls']} | {ag['iterations']} | `{ag['stop_reason']}` |")

    if save_trace:
        md_parts.insert(9, "## Bảng tổng kết\n\n" + "\n".join(summary_rows) + "\n")
        path = save_trace if os.path.isabs(save_trace) else os.path.join(BASE_DIR, save_trace)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(md_parts))
        print(f"\n💾 Đã lưu nhật ký trace vào: {os.path.relpath(path, BASE_DIR)}")

    return results


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Trợ Lý Duyệt Chi Phí Doanh Nghiệp — Chatbot vs ReAct Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--case", type=int, help="Chạy riêng một test case theo id")
    parser.add_argument("--all", action="store_true", help="Chạy toàn bộ test case")
    parser.add_argument("--mode", choices=["chatbot", "agent", "both"], default="agent",
                        help="Chế độ chạy (mặc định: agent)")
    parser.add_argument("--query", type=str, help="Chạy một câu hỏi tự do")
    parser.add_argument("--role", type=str, default="ketoan",
                        help="Vai trò: ketoan | nhanvien (mặc định: ketoan)")
    parser.add_argument("--user", type=str, default="", help="Mã nhân viên, ví dụ KT-01")
    parser.add_argument("--save-trace", type=str, default="",
                        help="Lưu nhật ký trace ra file markdown")
    parser.add_argument("--interactive", action="store_true",
                        help="Chế độ hội thoại, tự duyệt giao dịch thủ công")

    parser.add_argument("--init-db", action="store_true", help="Tạo/seed database")
    parser.add_argument("--reset-db", action="store_true", help="Xoá và seed lại database")
    parser.add_argument("--check-ocr", action="store_true", help="Kiểm tra kết nối OCR")
    parser.add_argument("--report", nargs=2, metavar=("KỲ", "GIÁ_TRỊ"),
                        help="Xuất báo cáo nhanh, ví dụ: --report month 2026-07")
    parser.add_argument("--audit", action="store_true", help="Xem nhật ký kiểm toán")
    parser.add_argument("--audit-blocked", action="store_true",
                        help="Chỉ xem các hành động BỊ CHẶN")
    parser.add_argument("--export-flowchart", action="store_true",
                        help="Sinh docs/hybrid_flowchart.mermaid từ graph")

    args = parser.parse_args()

    # ---------- Các lệnh tiện ích không cần LLM ----------
    if args.reset_db:
        banner("🗄️  RESET DATABASE")
        print(db.init_db(reset=True))
        return
    if args.init_db:
        banner("🗄️  KHỞI TẠO DATABASE")
        print(db.init_db())
        return
    if args.check_ocr:
        cmd_check_ocr()
        return
    if args.report:
        cmd_report(args.report[0], args.report[1])
        return
    if args.audit or args.audit_blocked:
        cmd_audit(limit=30, only_blocked=args.audit_blocked)
        return
    if args.export_flowchart:
        cmd_export_flowchart()
        return

    # ---------- Khởi động app ----------
    banner("🏫 VINUNI LAB 03 — TRỢ LÝ DUYỆT CHI PHÍ DOANH NGHIỆP")
    print("   Chatbot (Cấp 2)  vs  ReAct Agent (Cấp 3 — LangGraph 6 node)")

    print(f"\n{db.init_db()}")

    provider = get_llm_provider()
    model = getattr(provider, "model_name", "Offline Mock Mode")
    print(f"🔌 LLM Provider: {provider.__class__.__name__} (model: {model})")

    if provider.__class__.__name__ == "MockProvider":
        print("⚠️  CẢNH BÁO: Đang dùng MockProvider. Hãy tạo file .env với "
              "LLM_PROVIDER=gemini và GEMINI_API_KEY để chạy LLM thật.")

    ocr_state = ocr_client.check_ocr_connection()
    print(f"📷 Service OCR : {'✅ ' if ocr_state['ok'] else '❌ '}{ocr_state['message'][:100]}")
    print(f"💰 Số dư công ty (giả lập): {bank_api.format_money(bank_api.get_balance())}")
    print(f"🛡️  Guardrails  : MAX_ITERATIONS={prompts.MAX_ITERATIONS}, "
          f"MAX_REPEATED_ACTIONS={prompts.MAX_REPEATED_ACTIONS}, "
          f"ngưỡng duyệt tay={db.format_money(tool_module.HIGH_VALUE_THRESHOLD)}")

    role = roles.normalize_role(args.role)
    user_id = args.user or ("KT-01" if role == roles.KE_TOAN else "EMP-01")

    # ---------- Chế độ hội thoại ----------
    if args.interactive:
        cmd_interactive(provider, role, user_id)
        return

    # ---------- Câu hỏi tự do ----------
    if args.query:
        if args.mode in ("chatbot", "both"):
            run_baseline_chatbot(args.query, provider)
        if args.mode in ("agent", "both"):
            run_react_agent(args.query, provider, role=role, user_id=user_id,
                            approval_callback=cli_approval_callback)
        return

    # ---------- Test cases ----------
    cases = load_test_cases()
    print(f"✅ Đã tải {len(cases)} test case từ config/test_cases.json")

    if args.case:
        selected = [c for c in cases if c["id"] == args.case]
        if not selected:
            print(f"❌ Không tìm thấy test case #{args.case}. "
                  f"Các id hợp lệ: {', '.join(str(c['id']) for c in cases)}")
            return
    elif args.all:
        selected = cases
    else:
        # Mặc định: chạy test case #4 (báo cáo quý) — chạy được kể cả khi OCR chưa sẵn sàng
        selected = [c for c in cases if c["id"] == 4] or cases[:1]
        print("ℹ️  Chạy demo mặc định (test case #4). Dùng --all để chạy toàn bộ.")

    run_test_cases(selected, provider, args.mode, args.save_trace,
                   interactive_approval=args.interactive)


if __name__ == "__main__":
    main()
