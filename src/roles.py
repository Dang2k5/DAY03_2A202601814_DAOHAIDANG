"""
🔐 PHÂN QUYỀN & VAI TRÒ NGƯỜI DÙNG
Hiện thực hoá quy định QD-TC-04/2026 (data/policies/04_phan_quyen_uy_quyen.md).

⚠️ ĐÂY LÀ LỚP PHÒNG THỦ SỐ 2 trong 4 lớp Guardrails:
   Lớp 1 Moderation → **Lớp 2 Phân quyền** → Lớp 3 Vòng lặp → Lớp 4 Tầng tool + Human approval

Nguyên tắc đặc quyền tối thiểu (Điều 5, QD-TC-04): danh sách tool đưa vào System Prompt
được LỌC THEO VAI ngay từ đầu. Nhân viên thường KHÔNG HỀ BIẾT tool `transfer_payment` tồn tại.
Kể cả khi bị prompt injection ép gọi thẳng, tầng code này vẫn chặn và ghi audit log.
"""

# =============================================================================
# 1. ĐỊNH NGHĨA VAI TRÒ
# =============================================================================

KE_TOAN = "ke_toan"
NHAN_VIEN = "nhan_vien"

ROLES = {
    KE_TOAN: {
        "code": KE_TOAN,
        "label": "👔 Kế toán",
        "description": (
            "Cán bộ Phòng Tài chính - Kế toán. Xử lý toàn bộ vòng đời hóa đơn: "
            "nhận dạng, đối chiếu chính sách, truy vấn sổ cái, duyệt phiếu đề nghị, "
            "thực hiện chuyển khoản và kết xuất báo cáo tài chính."
        ),
    },
    NHAN_VIEN: {
        "code": NHAN_VIEN,
        "label": "👤 Nhân viên",
        "description": (
            "Cán bộ nhân viên các phòng ban. Chỉ được nộp hóa đơn của chính mình, "
            "tra cứu chính sách, tự đối chiếu tuân thủ, lập phiếu đề nghị thanh toán "
            "và xem trạng thái hóa đơn do chính mình nộp."
        ),
    },
}

DEFAULT_ROLE = NHAN_VIEN


# =============================================================================
# 2. MA TRẬN PHÂN QUYỀN TOOL
# =============================================================================
# Nguồn: brainstorm.md §5 — Ma trận 11 tool
# ✅ = được phép   ❌ = bị cấm (không hiện trong prompt, gọi thẳng cũng bị chặn)

ROLE_TOOL_PERMISSIONS = {
    KE_TOAN: {
        "list_invoice_files",
        "ocr_invoice",
        "search_policy",
        "check_policy_compliance",
        "check_duplicate_invoice",
        "query_finance_db",
        "get_my_invoice_status",
        "create_payment_ticket",
        "approve_ticket",
        "transfer_payment",
        "generate_business_report",
    },
    NHAN_VIEN: {
        "list_invoice_files",
        "ocr_invoice",
        "search_policy",
        "check_policy_compliance",
        "check_duplicate_invoice",
        "get_my_invoice_status",
        "create_payment_ticket",
    },
}

# Các tool có tác dụng phụ (ghi dữ liệu) — luôn phải ghi vào audit_log
WRITE_TOOLS = {
    "create_payment_ticket",
    "approve_ticket",
    "transfer_payment",
}

# Tool BẮT BUỘC phải qua Human-in-the-loop trước khi thực thi (Điều 6, QC-TC-01)
HUMAN_APPROVAL_TOOLS = {
    "transfer_payment",
}


# =============================================================================
# 3. HÀM KIỂM TRA QUYỀN
# =============================================================================

def is_valid_role(role: str) -> bool:
    """Kiểm tra mã vai trò có hợp lệ không."""
    return role in ROLES


def normalize_role(role: str) -> str:
    """
    Chuẩn hoá chuỗi vai trò người dùng nhập (chấp nhận nhiều cách viết).

    Ví dụ: 'ketoan', 'KE_TOAN', 'kế toán', 'accountant' -> 'ke_toan'
    """
    if not role:
        return DEFAULT_ROLE

    normalized = role.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "ketoan": KE_TOAN,
        "ke_toan": KE_TOAN,
        "kế_toán": KE_TOAN,
        "accountant": KE_TOAN,
        "nhanvien": NHAN_VIEN,
        "nhan_vien": NHAN_VIEN,
        "nhân_viên": NHAN_VIEN,
        "employee": NHAN_VIEN,
        "staff": NHAN_VIEN,
        "otherwise": NHAN_VIEN,
    }
    return aliases.get(normalized, DEFAULT_ROLE)


def get_allowed_tools(role: str) -> set:
    """
    Trả về TẬP HỢP tên tool mà vai trò này được phép sử dụng.

    Args:
        role (str): Mã vai trò ('ke_toan' hoặc 'nhan_vien')

    Returns:
        set: Tập tên tool được phép. Vai không hợp lệ -> trả về tập rỗng (fail-safe).
    """
    return ROLE_TOOL_PERMISSIONS.get(normalize_role(role), set())


def can_use_tool(role: str, tool_name: str) -> bool:
    """Vai trò này có được gọi tool đó không?"""
    return tool_name in get_allowed_tools(role)


def requires_human_approval(tool_name: str) -> bool:
    """Tool này có bắt buộc phải qua bước người duyệt không?"""
    return tool_name in HUMAN_APPROVAL_TOOLS


def is_write_tool(tool_name: str) -> bool:
    """Tool này có làm thay đổi dữ liệu không?"""
    return tool_name in WRITE_TOOLS


def permission_denied_message(role: str, tool_name: str) -> str:
    """
    Thông báo từ chối khi vai trò vượt quyền.

    Cố tình KHÔNG tiết lộ chi tiết tool bị cấm làm gì — tránh giúp kẻ tấn công dò tìm.
    """
    label = ROLES.get(normalize_role(role), {}).get("label", role)
    allowed = sorted(get_allowed_tools(role))
    return (
        f"LỖI PHÂN QUYỀN: Vai trò {label} KHÔNG có thẩm quyền sử dụng chức năng "
        f"'{tool_name}' (chiếu theo Điều 4, QD-TC-04/2026). Sự việc đã được ghi vào "
        f"nhật ký kiểm toán.\n"
        f"Các chức năng bạn được phép dùng: {', '.join(allowed)}.\n"
        f"Nếu cần thực hiện khoản chi này, hãy dùng 'create_payment_ticket' để lập "
        f"phiếu đề nghị gửi Phòng Tài chính - Kế toán."
    )


# =============================================================================
# 4. DANH SÁCH NGƯỜI DÙNG GIẢ LẬP (cho demo & Streamlit)
# =============================================================================

MOCK_USERS = {
    "KT-01": {"name": "Trần Thị Kế Toán", "role": KE_TOAN, "department": "Tài chính"},
    "KT-02": {"name": "Vũ Minh Sổ Sách", "role": KE_TOAN, "department": "Tài chính"},
    "EMP-01": {"name": "Nguyễn Văn An", "role": NHAN_VIEN, "department": "Marketing"},
    "EMP-02": {"name": "Trần Thanh Bình", "role": NHAN_VIEN, "department": "Sales"},
    "EMP-03": {"name": "Lê Thị Chi", "role": NHAN_VIEN, "department": "Kỹ thuật"},
    "EMP-04": {"name": "Phạm Tiến Dũng", "role": NHAN_VIEN, "department": "Marketing"},
}


def get_user(user_id: str) -> dict:
    """Tra cứu thông tin người dùng giả lập. Không tìm thấy -> trả về dict rỗng."""
    return MOCK_USERS.get(user_id, {})


def get_users_by_role(role: str) -> dict:
    """Lọc danh sách người dùng theo vai trò (phục vụ dropdown đăng nhập ở Streamlit)."""
    target = normalize_role(role)
    return {uid: info for uid, info in MOCK_USERS.items() if info["role"] == target}


# =============================================================================
# 5. SMOKE TEST
# =============================================================================

if __name__ == "__main__":
    import sys

    if sys.stdout.encoding != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    print("=" * 70)
    print("🔐 KIỂM TRA MA TRẬN PHÂN QUYỀN")
    print("=" * 70)

    all_tools = sorted(ROLE_TOOL_PERMISSIONS[KE_TOAN])
    print(f"\n{'Tool':<28} {'Kế toán':^10} {'Nhân viên':^12} {'Ghi':^6} {'Duyệt tay':^10}")
    print("-" * 70)
    for tool in all_tools:
        kt = "✅" if can_use_tool(KE_TOAN, tool) else "❌"
        nv = "✅" if can_use_tool(NHAN_VIEN, tool) else "❌"
        write = "✍️" if is_write_tool(tool) else "  "
        hitl = "🖐️" if requires_human_approval(tool) else "  "
        print(f"{tool:<28} {kt:^10} {nv:^12} {write:^6} {hitl:^10}")

    print("\n--- Test chuẩn hoá tên vai trò ---")
    for raw in ["ketoan", "KE_TOAN", "accountant", "nhanvien", "otherwise", "hacker"]:
        print(f"  '{raw}' -> '{normalize_role(raw)}'")

    print("\n--- Test chặn leo thang đặc quyền (Test Case #8) ---")
    print(permission_denied_message(NHAN_VIEN, "transfer_payment"))

    print("\n✅ roles.py hoạt động bình thường.")
