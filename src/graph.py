"""
🕸️ LANGGRAPH STATE MACHINE — 6 NODE
(Role 4: Core Developer / Integrator)

⚠️ GHI CHÚ QUAN TRỌNG VỀ THIẾT KẾ:
LangGraph có sẵn `create_react_agent()` làm hộ toàn bộ vòng lặp ReAct. Ở đây
CỐ TÌNH KHÔNG DÙNG hàm đó. LangGraph chỉ được dùng để điều phối node và edge;
còn phần lõi — parse `Thought:` / `Action:`, cắt bỏ `Observation:` do LLM bịa,
dispatch tool, đếm vòng lặp — đều là code tự viết trong file này, để nhìn thấy
rõ vòng lặp Thought → Action → Observation.

SƠ ĐỒ 6 NODE:

    START
      ↓
    ① moderation      Lớp 1: chặn prompt injection ngay cửa vào
      ↓ (SAFE)
    ② supervisor      Định tuyến DIRECT/REACT + kiểm tra quyền theo vai
      ↓                    ↓
    ③ direct_answer   ④ call_llm  ⇄  ⑤ tools     ← VÒNG LẶP ReAct
      ↓                    ↓           ↓
      ↓                    ↓      ⑥ human_approval  Lớp 4: chặn trước khi chuyển tiền
      ↓                    ↓           ↓
      └──────────────→   END   ←───────┘

Chạy độc lập:  python src/graph.py
"""

import json
import os
import re
import sys
from typing import Annotated, Any, Optional, TypedDict

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from langgraph.graph import END, START, StateGraph

import database as db
import prompts
import roles
import tools as tool_module

# =============================================================================
# TRẠNG THÁI CỦA GRAPH
# =============================================================================


class AgentState(TypedDict, total=False):
    """Trạng thái truyền giữa các node."""

    # --- Đầu vào ---
    user_query: str
    role: str
    user_id: str

    # --- Điều phối ---
    route: str                  # DIRECT | REACT | REFUSE
    moderation_label: str       # SAFE | INJECTION | ABUSE

    # --- Vòng lặp ReAct ---
    scratchpad: str             # lịch sử Thought/Action/Observation nối dồn
    steps: list                 # [{step, thought, action, tool, args, observation}]
    iterations: int
    tool_calls: int
    action_history: list        # ["tool|args", ...] để phát hiện lặp

    # --- Human-in-the-loop ---
    pending_action: Optional[dict]
    human_decision: str         # "" | APPROVED | REJECTED

    # --- Kết quả ---
    final_answer: str
    stop_reason: str            # FINAL_ANSWER | MAX_ITERATIONS | REPEATED_ACTION
                                # | PROVIDER_ERROR | MODERATION_BLOCKED | HUMAN_REJECTED


# =============================================================================
# 🧩 PARSER — PHẦN LÕI TỰ VIẾT (KHÔNG DÙNG THƯ VIỆN)
# =============================================================================

# Regex bắt `tên_tool[tham số]`
_ACTION_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\[(.*)\]\s*$", re.DOTALL)

# Nhận diện lỗi provider để dừng sớm thay vì lặp vô ích
_PROVIDER_ERROR_PREFIXES = ("[Gemini Error]", "[Gemini Exception]", "[OpenAI Error]",
                            "[OpenAI Exception]", "[Anthropic Error]",
                            "[Anthropic Exception]", "[OpenRouter", "[Provider")


def is_provider_error(text: str) -> bool:
    """Phản hồi này có phải lỗi từ nhà cung cấp LLM không?"""
    return bool(text) and str(text).strip().startswith(_PROVIDER_ERROR_PREFIXES)


def strip_code_fences(text: str) -> str:
    """
    Gỡ bỏ khối ```...``` mà Gemini rất hay tự bọc quanh output.

    Không gỡ thì regex Action không khớp được vì dòng cuối là ``` chứ không phải `]`.
    """
    if not text:
        return ""
    text = re.sub(r"^\s*```[a-zA-Z]*\s*\n", "", text)
    text = re.sub(r"\n\s*```\s*$", "", text)
    return text.replace("```", "").strip()


def parse_llm_output(raw: str) -> dict:
    """
    Tách phản hồi LLM thành Thought / Action / Final Answer.

    🔑 BẤT BIẾN SỐ 2 CỦA REACT: nếu LLM tự bịa dòng `Observation:`, TOÀN BỘ phần
    từ đó trở đi bị CẮT BỎ. Chỉ ứng dụng mới được quyền viết Observation, sau khi
    thực sự chạy tool. Không có bước cắt này, Agent sẽ "ảo giác" ra kết quả tool
    và đưa ra kết luận dựa trên dữ liệu không tồn tại.

    Returns:
        dict: {thought, action, final_answer, hallucinated_observation, cleaned}
    """
    text = strip_code_fences(raw or "")

    # --- Cắt bỏ Observation do LLM tự bịa ---
    hallucinated = ""
    obs_match = re.search(r"^\s*Observation\s*:", text, re.MULTILINE | re.IGNORECASE)
    if obs_match:
        hallucinated = text[obs_match.start():].strip()
        text = text[:obs_match.start()].strip()

    def _extract(label: str) -> str:
        """Lấy nội dung sau `Label:` cho tới nhãn kế tiếp hoặc hết chuỗi."""
        pattern = (rf"^\s*{label}\s*:\s*(.*?)"
                   rf"(?=^\s*(?:Thought|Action|Final\s*Answer|Observation)\s*:|\Z)")
        m = re.search(pattern, text, re.MULTILINE | re.IGNORECASE | re.DOTALL)
        return m.group(1).strip() if m else ""

    final_answer = _extract(r"Final\s*Answer")
    action = _extract("Action")
    thought = _extract("Thought")

    # LLM đôi khi bỏ hẳn nhãn và trả lời thẳng ➔ coi như Final Answer
    if not final_answer and not action and text and not thought:
        final_answer = text.strip()

    return {
        "thought": thought,
        "action": action,
        "final_answer": final_answer,
        "hallucinated_observation": hallucinated,
        "cleaned": text,
    }


def split_args(raw_args: str) -> list:
    """
    Tách tham số bên trong ngoặc vuông, TÔN TRỌNG dấu nháy.

    'a", "b, c"'  ->  ['a', 'b, c']   (dấu phẩy trong nháy KHÔNG bị tách)

    Tự viết thay vì dùng split(',') vì tên nhà cung cấp và lý do rất hay chứa dấu phẩy.
    """
    args, current = [], []
    quote = None
    i = 0
    while i < len(raw_args):
        ch = raw_args[i]
        if quote:
            if ch == "\\" and i + 1 < len(raw_args):    # ký tự escape
                current.append(raw_args[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            else:
                current.append(ch)
        else:
            if ch in ("'", '"', "“", "”", "‘", "’"):
                quote = {"“": "”", "”": "”", "‘": "’", "’": "’"}.get(ch, ch)
            elif ch == ",":
                args.append("".join(current).strip())
                current = []
            else:
                current.append(ch)
        i += 1

    args.append("".join(current).strip())
    return [a for a in args if a != ""] if len(args) > 1 else \
           ([args[0]] if args and args[0] else [])


def parse_action(action_str: str):
    """
    Tách `tên_tool[arg1, arg2]` thành (tool_name, [args]).

    Returns:
        (str, list) nếu hợp lệ, hoặc (None, thông_báo_lỗi) nếu sai cú pháp.

    Xử lý được các biến thể LLM hay viết sai:
      • Thiếu ngoặc đóng            -> tự thêm nếu có ngoặc mở
      • Dùng ngoặc tròn thay vuông  -> chuyển đổi
      • Bọc thêm dấu backtick       -> gỡ
    """
    if not action_str or not action_str.strip():
        return None, "Dòng Action rỗng."

    s = action_str.strip().strip("`").strip()
    s = s.splitlines()[0].strip() if "\n" in s else s

    # LLM đôi khi viết tool_name(...) thay vì tool_name[...]
    if "[" not in s and "(" in s and s.rstrip().endswith(")"):
        s = s.replace("(", "[", 1)
        s = s[::-1].replace(")", "]", 1)[::-1]

    # Thiếu ngoặc đóng ➔ tự vá
    if "[" in s and "]" not in s:
        s = s + "]"

    # Không có ngoặc gì cả ➔ coi là gọi tool không tham số
    if "[" not in s:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", s):
            return s, []
        return None, f"Không nhận ra cú pháp công cụ trong: {action_str!r}"

    m = _ACTION_RE.match(s)
    if not m:
        return None, f"Không tách được tên công cụ và tham số từ: {action_str!r}"

    tool_name = m.group(1).strip()
    args = split_args(m.group(2).strip())
    return tool_name, args


# =============================================================================
# 🔧 EXECUTOR — GỌI TOOL AN TOÀN
# =============================================================================

def execute_tool(tool_name: str, args: list, role: str, user_id: str) -> str:
    """
    Gọi tool sau khi kiểm tra quyền. Luôn trả về chuỗi Observation.

    LỚP PHÒNG THỦ SỐ 2 nằm ở đây: dù LLM có bị dụ gọi tool vượt quyền,
    hàm này vẫn chặn ở tầng code và ghi vào nhật ký kiểm toán.
    """
    allowed = tool_module.get_tools_for_role(role)

    # --- Chặn vượt quyền ---
    if tool_name not in allowed:
        exists_globally = tool_name in tool_module.AVAILABLE_TOOLS
        db.write_audit(
            tool=tool_name, role=role, user_id=user_id, args=str(args),
            blocked_reason=("Vượt quyền: vai không được dùng tool này"
                            if exists_globally else "Gọi tool không tồn tại"),
        )
        if exists_globally:
            return roles.permission_denied_message(role, tool_name)
        return prompts.UNKNOWN_TOOL_TEMPLATE.format(
            tool_name=tool_name, allowed_tools=", ".join(sorted(allowed))
        )

    fn = allowed[tool_name]

    # --- Gọi tool, bọc mọi ngoại lệ ---
    try:
        return str(fn(*args))
    except TypeError as e:
        spec = tool_module.TOOL_SPECS.get(tool_name, {})
        expected = [a[0] for a in spec.get("args", [])]
        return (f"LỖI THAM SỐ: Gọi {tool_name} với {len(args)} tham số nhưng không khớp "
                f"chữ ký hàm.\n"
                f"Chữ ký đúng: {tool_name}[{', '.join(expected)}]\n"
                f"Ví dụ đúng: {spec.get('example', '(không có ví dụ)')}\n"
                f"Chi tiết kỹ thuật: {e}")
    except Exception as e:
        db.write_audit(tool=tool_name, role=role, user_id=user_id, args=str(args),
                       blocked_reason=f"Exception: {type(e).__name__}")
        return (f"LỖI HỆ THỐNG khi chạy {tool_name}: {type(e).__name__}: {e}\n"
                f"Hãy thử công cụ khác hoặc báo lại người dùng.")


# =============================================================================
# CẤU HÌNH RUNTIME (provider + hàm hỏi người duyệt)
# =============================================================================

_RUNTIME = {
    "provider": None,
    "approval_callback": None,   # fn(pending_action: dict) -> bool
    "verbose": True,
}


def configure(provider=None, approval_callback=None, verbose: bool = True):
    """Nạp LLM provider và hàm xử lý bước người duyệt (CLI hỏi y/n, Streamlit hiện nút)."""
    if provider is not None:
        _RUNTIME["provider"] = provider
    if approval_callback is not None:
        _RUNTIME["approval_callback"] = approval_callback
    _RUNTIME["verbose"] = verbose


def _log(msg: str):
    if _RUNTIME["verbose"]:
        print(msg)


def _llm(prompt: str, system_prompt: str = "") -> str:
    provider = _RUNTIME["provider"]
    if provider is None:
        return "[Provider Error]: Chưa cấu hình LLM provider. Gọi graph.configure(provider=...)."
    try:
        return provider.generate(prompt, system_prompt=system_prompt)
    except Exception as e:
        return f"[Provider Exception]: {type(e).__name__}: {e}"


# =============================================================================
# ① NODE MODERATION — LỚP PHÒNG THỦ SỐ 1
# =============================================================================

def node_moderation(state: AgentState) -> dict:
    """
    Kiểm duyệt đầu vào TRƯỚC KHI câu hỏi chạm tới Agent.

    Hai tầng:
      1. Luật (blocklist)  — nhanh, miễn phí, bắt các mẫu tấn công phổ biến
      2. LLM classifier    — bắt biến thể tinh vi hơn mà luật bỏ sót
    """
    query = state["user_query"]
    lowered = query.lower()

    # --- Tầng 1: luật ---
    matched = next((p for p in prompts.INJECTION_PATTERNS if p in lowered), None)
    if matched:
        _log(f"🛡️  [MODERATION] CHẶN — khớp mẫu tấn công: '{matched}'")
        db.write_audit(tool="node_moderation", role=state["role"], user_id=state["user_id"],
                       args=query[:200], result="INJECTION",
                       blocked_reason=f"Prompt injection, khớp mẫu '{matched}'")
        return {"moderation_label": "INJECTION", "route": "REFUSE",
                "final_answer": prompts.MODERATION_REFUSAL,
                "stop_reason": "MODERATION_BLOCKED"}

    # --- Tầng 2: LLM classifier ---
    verdict = _llm(prompts.MODERATION_PROMPT.format(user_query=query)).strip().upper()

    if is_provider_error(verdict):
        # Không phân loại được thì CHO QUA — các lớp 2/3/4 vẫn còn nguyên,
        # không nên chặn nhầm người dùng hợp lệ chỉ vì LLM lỗi.
        _log("🛡️  [MODERATION] Không gọi được LLM, cho qua (các lớp phòng thủ sau vẫn hoạt động)")
        return {"moderation_label": "SAFE"}

    label = next((l for l in ("INJECTION", "ABUSE", "SAFE") if l in verdict), "SAFE")

    if label == "INJECTION":
        _log("🛡️  [MODERATION] CHẶN — LLM phân loại: INJECTION")
        db.write_audit(tool="node_moderation", role=state["role"], user_id=state["user_id"],
                       args=query[:200], result="INJECTION",
                       blocked_reason="Prompt injection (LLM classifier)")
        return {"moderation_label": "INJECTION", "route": "REFUSE",
                "final_answer": prompts.MODERATION_REFUSAL,
                "stop_reason": "MODERATION_BLOCKED"}

    if label == "ABUSE":
        _log("🛡️  [MODERATION] CHẶN — LLM phân loại: ABUSE")
        db.write_audit(tool="node_moderation", role=state["role"], user_id=state["user_id"],
                       args=query[:200], result="ABUSE", blocked_reason="Nội dung không phù hợp")
        return {"moderation_label": "ABUSE", "route": "REFUSE",
                "final_answer": prompts.ABUSE_REFUSAL,
                "stop_reason": "MODERATION_BLOCKED"}

    _log("🛡️  [MODERATION] SAFE — cho qua")
    return {"moderation_label": "SAFE"}


# =============================================================================
# ② NODE SUPERVISOR — ĐỊNH TUYẾN (hiện thực của Hybrid Flowchart)
# =============================================================================

# Dấu hiệu chắc chắn cần dữ liệu thật ➔ khỏi tốn 1 lượt gọi LLM để phân loại
_REACT_SIGNALS = [
    "data/invoices", ".jpg", ".jpeg", ".png", ".pdf",
    "hd-2026", "hóa đơn số", "ma so thue",
    "báo cáo", "bao cao", "thống kê", "thong ke",
    "thanh toán", "thanh toan", "chuyển khoản", "chuyen khoan",
    "hạn mức", "han muc", "ngân sách", "ngan sach",
    "công ty mình", "cong ty minh", "nội bộ", "noi bo",
    "quý 1", "quý 2", "quý 3", "quý 4", "quy 1", "quy 2", "quy 3", "quy 4",
    "phiếu đề nghị", "phieu de nghi", "ticket",
    "trạng thái", "trang thai", "của tôi", "cua toi",
]


def node_supervisor(state: AgentState) -> dict:
    """
    Quyết định câu hỏi đi đường Chatbot (DIRECT) hay đường ReAct Agent (REACT).

    Đây chính là hiện thực bằng code của sơ đồ docs/hybrid_flowchart.mermaid.
    """
    query = state["user_query"]
    lowered = query.lower()

    # --- Tầng 1: luật ---
    signal = next((s for s in _REACT_SIGNALS if s in lowered), None)
    if signal:
        _log(f"🧭 [SUPERVISOR] → REACT (khớp dấu hiệu dữ liệu thật: '{signal}')")
        return {"route": "REACT"}

    # --- Tầng 2: LLM phân loại ---
    verdict = _llm(prompts.SUPERVISOR_PROMPT.format(user_query=query)).strip().upper()

    if is_provider_error(verdict):
        _log("🧭 [SUPERVISOR] → REACT (LLM lỗi, chọn đường an toàn hơn)")
        return {"route": "REACT"}

    route = "DIRECT" if "DIRECT" in verdict else "REACT"
    _log(f"🧭 [SUPERVISOR] → {route} (LLM phân loại)")
    return {"route": route}


# =============================================================================
# ③ NODE DIRECT ANSWER — Chatbot path (Cấp 2)
# =============================================================================

def node_direct_answer(state: AgentState) -> dict:
    """Trả lời bằng đúng MỘT lần gọi LLM, không dùng tool. Nhanh và rẻ hơn ReAct."""
    _log("💬 [DIRECT] Trả lời bằng 1 lần gọi LLM, không dùng công cụ")
    answer = _llm(state["user_query"], system_prompt=prompts.CHATBOT_BASELINE_PROMPT)

    if is_provider_error(answer):
        return {"final_answer": prompts.PROVIDER_ERROR_MESSAGE.format(error=answer),
                "stop_reason": "PROVIDER_ERROR"}
    return {"final_answer": answer, "stop_reason": "FINAL_ANSWER"}


# =============================================================================
# ④ NODE CALL LLM — sinh Thought/Action (một bước của vòng lặp ReAct)
# =============================================================================

def node_call_llm(state: AgentState) -> dict:
    """
    Gọi LLM để sinh bước suy luận tiếp theo, rồi PARSE bằng parser tự viết.

    LỚP PHÒNG THỦ SỐ 3 (một phần): cắt bỏ Observation do LLM bịa ra.
    """
    iterations = state.get("iterations", 0) + 1
    steps = list(state.get("steps", []))

    _log(f"\n{'─' * 66}")
    _log(f"🔄 VÒNG LẶP ReAct — Bước {iterations}/{prompts.MAX_ITERATIONS}")
    _log(f"{'─' * 66}")

    system_prompt = prompts.build_react_system_prompt(state["role"], state["user_id"])
    user_prompt = f"Question: {state['user_query']}\n\n{state.get('scratchpad', '')}"

    raw = _llm(user_prompt, system_prompt=system_prompt)

    # --- Provider lỗi ➔ dừng NGAY, không lặp vô ích ---
    if is_provider_error(raw):
        _log(f"❌ [CALL_LLM] Lỗi provider: {raw[:120]}")
        return {"iterations": iterations, "stop_reason": "PROVIDER_ERROR",
                "final_answer": prompts.PROVIDER_ERROR_MESSAGE.format(error=raw)}

    parsed = parse_llm_output(raw)

    if parsed["thought"]:
        _log(f"🧠 Thought: {parsed['thought'][:400]}")

    if parsed["hallucinated_observation"]:
        _log(f"✂️  [GUARDRAIL] LLM tự bịa Observation — đã CẮT BỎ "
             f"{len(parsed['hallucinated_observation'])} ký tự")

    # --- Có Final Answer ➔ kết thúc ---
    if parsed["final_answer"]:
        _log(f"🏁 Final Answer: {parsed['final_answer'][:400]}")
        steps.append({"step": iterations, "thought": parsed["thought"],
                      "action": "", "tool": "", "args": [],
                      "observation": "", "final_answer": parsed["final_answer"]})
        return {"iterations": iterations, "steps": steps,
                "final_answer": parsed["final_answer"], "stop_reason": "FINAL_ANSWER",
                "pending_action": None}

    # --- Không có cả Action lẫn Final Answer ➔ nhắc lại định dạng ---
    if not parsed["action"]:
        _log("⚠️  [GUARDRAIL] Không tìm thấy Action lẫn Final Answer — nhắc lại định dạng")
        scratchpad = (state.get("scratchpad", "")
                      + f"\nThought: {parsed['thought']}"
                      + f"\nObservation: {prompts.NO_ACTION_HINT}\n")
        return {"iterations": iterations, "scratchpad": scratchpad, "steps": steps,
                "pending_action": None}

    # --- Parse Action ---
    tool_name, args_or_err = parse_action(parsed["action"])

    if tool_name is None:
        _log(f"⚠️  [GUARDRAIL] Action sai cú pháp: {parsed['action'][:120]}")
        hint = prompts.MALFORMED_ACTION_HINT.format(raw_action=parsed["action"][:200])
        scratchpad = (state.get("scratchpad", "")
                      + f"\nThought: {parsed['thought']}"
                      + f"\nAction: {parsed['action']}"
                      + f"\nObservation: {hint}\n")
        steps.append({"step": iterations, "thought": parsed["thought"],
                      "action": parsed["action"], "tool": "", "args": [],
                      "observation": hint})
        return {"iterations": iterations, "scratchpad": scratchpad, "steps": steps,
                "pending_action": None}

    args = args_or_err
    _log(f"🛠️  Action: {tool_name}{args}")

    return {
        "iterations": iterations,
        "steps": steps,
        "pending_action": {"tool": tool_name, "args": args,
                           "thought": parsed["thought"], "raw_action": parsed["action"]},
    }


# =============================================================================
# ⑤ NODE TOOLS — thực thi công cụ, chèn Observation THẬT
# =============================================================================

def node_tools(state: AgentState) -> dict:
    """
    Chạy tool và nối Observation THẬT vào scratchpad.

    LỚP PHÒNG THỦ SỐ 3 (phần còn lại): phát hiện lặp cùng một hành động.
    """
    pending = state.get("pending_action") or {}
    tool_name, args = pending.get("tool", ""), pending.get("args", [])
    thought = pending.get("thought", "")

    steps = list(state.get("steps", []))
    history = list(state.get("action_history", []))
    signature = f"{tool_name}|{json.dumps(args, ensure_ascii=False)}"
    repeat_count = history.count(signature)

    # --- Chống lặp: đã gọi y hệt quá nhiều lần ---
    if repeat_count >= prompts.MAX_REPEATED_ACTIONS:
        _log(f"🛑 [GUARDRAIL] Đã gọi {tool_name} với cùng tham số {repeat_count} lần — NGẮT LẶP")
        db.write_audit(tool=tool_name, role=state["role"], user_id=state["user_id"],
                       args=str(args), blocked_reason=f"Lặp hành động {repeat_count} lần")
        progress = _summarize_progress(steps)
        return {
            "stop_reason": "REPEATED_ACTION",
            "action_history": history,
            "final_answer": (
                f"Tôi phát hiện mình đang lặp lại cùng một thao tác ({tool_name}) mà kết quả "
                f"không thay đổi, nên dừng lại để tránh vòng lặp vô hạn.\n\n"
                f"Những gì đã làm được:\n{progress}\n\n"
                f"Bạn vui lòng cung cấp thêm thông tin hoặc liên hệ Phòng Tài chính - Kế toán."
            ),
        }

    # --- Cảnh báo lặp lần đầu ---
    if repeat_count > 0:
        _log(f"⚠️  [GUARDRAIL] {tool_name} đã gọi {repeat_count} lần với cùng tham số")

    # --- Hành động cần người duyệt ➔ chuyển sang node human_approval ---
    if roles.requires_human_approval(tool_name) and state.get("human_decision") != "APPROVED":
        amount = _extract_amount_arg(tool_name, args)
        if amount >= tool_module.HIGH_VALUE_THRESHOLD:
            _log(f"🖐️  [HITL] {tool_name} với số tiền {db.format_money(amount)} "
                 f"≥ ngưỡng — CHUYỂN SANG BƯỚC NGƯỜI DUYỆT")
            return {"pending_action": {**pending, "amount": amount, "needs_approval": True},
                    "action_history": history}

    # --- Chạy tool thật ---
    observation = execute_tool(tool_name, args, state["role"], state["user_id"])
    _log(f"👁️  Observation: {observation[:500]}"
         + (" ..." if len(observation) > 500 else ""))

    history.append(signature)
    steps.append({"step": state.get("iterations", 0), "thought": thought,
                  "action": pending.get("raw_action", ""), "tool": tool_name,
                  "args": args, "observation": observation})

    scratchpad = (state.get("scratchpad", "")
                  + f"\nThought: {thought}"
                  + f"\nAction: {tool_name}[{', '.join(repr(a) for a in args)}]"
                  + f"\nObservation: {observation}\n")

    return {
        "scratchpad": scratchpad,
        "steps": steps,
        "action_history": history,
        "tool_calls": state.get("tool_calls", 0) + 1,
        "pending_action": None,
        "human_decision": "",       # reset sau khi đã dùng
    }


def _extract_amount_arg(tool_name: str, args: list) -> float:
    """Lấy giá trị tham số 'amount' theo vị trí đã khai báo trong TOOL_SPECS."""
    spec = tool_module.TOOL_SPECS.get(tool_name, {})
    names = [a[0] for a in spec.get("args", [])]
    if "amount" in names:
        idx = names.index("amount")
        if idx < len(args):
            try:
                return float(str(args[idx]).replace(".", "").replace(",", "").strip())
            except ValueError:
                return 0.0
    return 0.0


def _summarize_progress(steps: list) -> str:
    """Tóm tắt các bước đã chạy — dùng trong thông báo fallback."""
    done = [s for s in steps if s.get("tool")]
    if not done:
        return "  (chưa hoàn thành bước nào)"
    return "\n".join(
        f"  {i}. {s['tool']} → {str(s['observation']).splitlines()[0][:90]}"
        for i, s in enumerate(done, 1)
    )


# =============================================================================
# ⑥ NODE HUMAN APPROVAL — LỚP PHÒNG THỦ SỐ 4 (nguyên tắc bốn mắt)
# =============================================================================

def node_human_approval(state: AgentState) -> dict:
    """
    Dừng luồng, chờ CON NGƯỜI xác nhận trước khi chuyển tiền.

    Cờ phê duyệt được ghi vào tools._SESSION — NẰM NGOÀI TẦM VỚI CỦA LLM.
    Kể cả LLM có bịa ra tham số `approved_by` cũng vô nghĩa.
    """
    pending = state.get("pending_action") or {}
    args = pending.get("args", [])
    amount = pending.get("amount", 0)

    invoice_no = args[0] if len(args) > 0 else "(không rõ)"
    vendor = args[1] if len(args) > 1 else "(không rõ)"
    category = args[3] if len(args) > 3 else "(không rõ)"

    _log("\n" + prompts.HUMAN_APPROVAL_PROMPT.format(
        invoice_no=invoice_no, vendor=vendor,
        amount=db.format_money(amount), category=category,
        threshold=db.format_money(tool_module.HIGH_VALUE_THRESHOLD)))

    callback = _RUNTIME["approval_callback"]
    if callback is None:
        # Không có ai để hỏi ➔ mặc định TỪ CHỐI (fail-safe, không fail-open)
        approved = False
        _log("🚫 [HITL] Không có kênh xác nhận nào được cấu hình → TỪ CHỐI theo mặc định an toàn")
    else:
        try:
            approved = bool(callback({**pending, "invoice_no": invoice_no,
                                      "vendor": vendor, "amount": amount,
                                      "category": category, "role": state["role"],
                                      "user_id": state["user_id"]}))
        except Exception as e:
            approved = False
            _log(f"🚫 [HITL] Kênh xác nhận lỗi ({e}) → TỪ CHỐI theo mặc định an toàn")

    db.write_audit(tool="node_human_approval", role=state["role"], user_id=state["user_id"],
                   args=f"{invoice_no}|{amount}",
                   result="APPROVED" if approved else "REJECTED",
                   blocked_reason="" if approved else "Người duyệt từ chối")

    if approved:
        _log("✅ [HITL] Người duyệt ĐỒNG Ý — cấp phép cho đúng giao dịch này")
        tool_module.grant_approval(tool_module.approval_key(invoice_no, amount))
        return {"human_decision": "APPROVED",
                "pending_action": {k: v for k, v in pending.items() if k != "needs_approval"}}

    _log("🚫 [HITL] Người duyệt TỪ CHỐI — huỷ giao dịch")
    steps = list(state.get("steps", []))
    note = (f"Người có thẩm quyền đã TỪ CHỐI giao dịch {invoice_no} "
            f"({db.format_money(amount)}).")
    steps.append({"step": state.get("iterations", 0), "thought": pending.get("thought", ""),
                  "action": pending.get("raw_action", ""), "tool": "human_approval",
                  "args": args, "observation": note})

    return {
        "human_decision": "REJECTED",
        "stop_reason": "HUMAN_REJECTED",
        "steps": steps,
        "pending_action": None,
        "final_answer": (
            f"{note}\n\n"
            f"Giao dịch KHÔNG được thực hiện. Không có khoản tiền nào được chuyển đi.\n"
            f"Nếu khoản chi này vẫn cần xử lý, hãy lập phiếu đề nghị duyệt chi để chuyển "
            f"lên cấp có thẩm quyền xem xét."
        ),
    }


# =============================================================================
# ĐIỀU KIỆN RẼ NHÁNH
# =============================================================================

def route_after_moderation(state: AgentState) -> str:
    return "refuse" if state.get("route") == "REFUSE" else "supervisor"


def route_after_supervisor(state: AgentState) -> str:
    return "direct" if state.get("route") == "DIRECT" else "react"


def route_after_llm(state: AgentState) -> str:
    """Sau khi LLM sinh bước: kết thúc, chạy tool, hay hết budget?"""
    if state.get("stop_reason"):
        return "end"
    if state.get("iterations", 0) >= prompts.MAX_ITERATIONS:
        return "exhausted"
    if state.get("pending_action"):
        return "tools"
    return "continue"   # parse lỗi -> quay lại LLM với gợi ý sửa


def route_after_tools(state: AgentState) -> str:
    if state.get("stop_reason"):
        return "end"
    pending = state.get("pending_action") or {}
    if pending.get("needs_approval"):
        return "approval"
    if state.get("iterations", 0) >= prompts.MAX_ITERATIONS:
        return "exhausted"
    return "continue"


def route_after_approval(state: AgentState) -> str:
    return "tools" if state.get("human_decision") == "APPROVED" else "end"


def node_exhausted(state: AgentState) -> dict:
    """Hết ngân sách vòng lặp — trả lời lịch sự thay vì đoán bừa (Safe Fallback)."""
    _log(f"\n🛑 [GUARDRAIL] ĐẠT GIỚI HẠN {prompts.MAX_ITERATIONS} VÒNG LẶP — NGẮT AN TOÀN")
    return {
        "stop_reason": "MAX_ITERATIONS",
        "final_answer": prompts.SAFE_FALLBACK_MESSAGE.format(
            iterations=state.get("iterations", 0),
            progress=_summarize_progress(state.get("steps", [])),
        ),
    }


# =============================================================================
# XÂY DỰNG GRAPH
# =============================================================================

def build_graph():
    """Lắp 6 node thành StateGraph của LangGraph."""
    g = StateGraph(AgentState)

    g.add_node("moderation", node_moderation)
    g.add_node("supervisor", node_supervisor)
    g.add_node("direct_answer", node_direct_answer)
    g.add_node("call_llm", node_call_llm)
    g.add_node("tools", node_tools)
    g.add_node("human_approval", node_human_approval)
    g.add_node("exhausted", node_exhausted)

    g.add_edge(START, "moderation")

    g.add_conditional_edges("moderation", route_after_moderation,
                            {"refuse": END, "supervisor": "supervisor"})

    g.add_conditional_edges("supervisor", route_after_supervisor,
                            {"direct": "direct_answer", "react": "call_llm"})

    g.add_edge("direct_answer", END)

    g.add_conditional_edges("call_llm", route_after_llm,
                            {"end": END, "tools": "tools",
                             "continue": "call_llm", "exhausted": "exhausted"})

    g.add_conditional_edges("tools", route_after_tools,
                            {"end": END, "approval": "human_approval",
                             "continue": "call_llm", "exhausted": "exhausted"})

    g.add_conditional_edges("human_approval", route_after_approval,
                            {"tools": "tools", "end": END})

    g.add_edge("exhausted", END)

    return g.compile()


_COMPILED = None


def get_graph():
    """Lấy graph đã compile (chỉ build một lần)."""
    global _COMPILED
    if _COMPILED is None:
        _COMPILED = build_graph()
    return _COMPILED


def export_mermaid() -> str:
    """
    Xuất sơ đồ Mermaid TỪ CHÍNH GRAPH ĐANG CHẠY.

    Nhờ vậy docs/hybrid_flowchart.mermaid luôn khớp với code thật,
    không phải hình vẽ tay có thể lệch theo thời gian.
    """
    try:
        return get_graph().get_graph().draw_mermaid()
    except Exception as e:
        return f"%% Không xuất được sơ đồ: {e}"


# =============================================================================
# API CHÍNH
# =============================================================================

def run_agent(user_query: str, role: str = roles.KE_TOAN, user_id: str = "",
              provider=None, approval_callback=None, verbose: bool = True) -> dict:
    """
    Chạy toàn bộ graph cho một câu hỏi.

    Returns:
        dict: {question, role, user_id, route, moderation_label, final_answer,
               stop_reason, tool_calls, iterations, steps}
    """
    if provider is not None or approval_callback is not None:
        configure(provider=provider, approval_callback=approval_callback, verbose=verbose)
    else:
        _RUNTIME["verbose"] = verbose

    role = roles.normalize_role(role)
    tool_module.set_session(role, user_id)
    db.init_db()

    initial: AgentState = {
        "user_query": user_query, "role": role, "user_id": user_id,
        "route": "", "moderation_label": "", "scratchpad": "", "steps": [],
        "iterations": 0, "tool_calls": 0, "action_history": [],
        "pending_action": None, "human_decision": "",
        "final_answer": "", "stop_reason": "",
    }

    # recursion_limit: phanh cuối cùng của LangGraph, đặt rộng hơn MAX_ITERATIONS
    # vì mỗi vòng ReAct tiêu tốn 2-3 lượt chuyển node.
    final_state = get_graph().invoke(
        initial, config={"recursion_limit": prompts.MAX_ITERATIONS * 4 + 10}
    )

    return {
        "question": user_query,
        "role": role,
        "user_id": user_id,
        "route": final_state.get("route", ""),
        "moderation_label": final_state.get("moderation_label", ""),
        "final_answer": final_state.get("final_answer", ""),
        "stop_reason": final_state.get("stop_reason", "UNKNOWN"),
        "tool_calls": final_state.get("tool_calls", 0),
        "iterations": final_state.get("iterations", 0),
        "steps": final_state.get("steps", []),
    }


# =============================================================================
# SMOKE TEST — kiểm tra parser (KHÔNG cần API key)
# =============================================================================

if __name__ == "__main__":
    print("=" * 78)
    print("🕸️  KIỂM TRA LANGGRAPH & PARSER REACT")
    print("=" * 78)

    print("\n▶ SƠ ĐỒ GRAPH (sinh tự động từ code):")
    print(export_mermaid())

    print("\n" + "=" * 78)
    print("🧩 TEST PARSER — parse_llm_output()")
    print("=" * 78)

    samples = [
        ("Bình thường",
         'Thought: Cần tra chính sách trước.\nAction: search_policy["hạn mức tiếp khách"]'),
        ("LLM TỰ BỊA Observation (phải bị cắt)",
         'Thought: Tra cứu thôi.\nAction: search_policy["hạn mức"]\n'
         'Observation: Hạn mức là 5 triệu.\nThought: Vậy là đủ.\n'
         'Final Answer: Hạn mức 5 triệu.'),
        ("Bọc trong code fence",
         '```\nThought: Suy nghĩ.\nAction: list_invoice_files[]\n```'),
        ("Chỉ có Final Answer",
         'Thought: Đã đủ thông tin.\nFinal Answer: Tổng chi quý 2 là 24.170.000đ.'),
        ("Trả lời trần trụi không nhãn",
         'Chi phí được trừ là các khoản chi thực tế phát sinh...'),
    ]

    for label, raw in samples:
        p = parse_llm_output(raw)
        print(f"\n─── {label} ───")
        print(f"   Thought       : {p['thought'][:60] or '(rỗng)'}")
        print(f"   Action        : {p['action'][:60] or '(rỗng)'}")
        print(f"   Final Answer  : {p['final_answer'][:60] or '(rỗng)'}")
        if p["hallucinated_observation"]:
            print(f"   ✂️  ĐÃ CẮT BỎ : {p['hallucinated_observation'][:70]}...")

    print("\n" + "=" * 78)
    print("🧩 TEST PARSER — parse_action()")
    print("=" * 78)

    actions = [
        'search_policy["hạn mức tiếp khách"]',
        'check_policy_compliance["Tiếp khách", "4750000", "0101245789", "2026-07-02"]',
        'list_invoice_files[]',
        'create_payment_ticket["HD-01", "5000000", "Tài chính", "Vượt hạn mức, cần duyệt"]',
        'search_policy["thiếu ngoặc đóng"',
        'search_policy("dùng ngoặc tròn")',
        'transfer_payment[“ngoặc kép cong”, "Vendor A", "850000", "Đi lại"]',
        'không phải cú pháp tool nào cả',
    ]
    for a in actions:
        name, res = parse_action(a)
        if name is None:
            print(f"\n   ❌ {a[:62]}\n      → {res[:80]}")
        else:
            print(f"\n   ✅ {a[:62]}\n      → tool='{name}'  args={res}")

    print("\n" + "=" * 78)
    print("✅ graph.py — parser và graph hoạt động (chưa gọi LLM thật).")
    print("=" * 78)
