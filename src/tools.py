"""
🛠️ TOOL REGISTRY — 11 CÔNG CỤ CHO REACT AGENT
(Role 2: Tool & Spec Engineer)

BỐN NGUYÊN TẮC BẤT BIẾN khi viết tool:
  1. Tool LUÔN trả về `str` — vì Observation phải là text để chèn vào prompt.
  2. Tool KHÔNG BAO GIỜ raise — lỗi nghiệp vụ là DỮ LIỆU để Agent suy luận đổi hướng,
     không phải sự cố làm sập chương trình.
  3. Thông báo lỗi phải NÓI RÕ PHẢI LÀM GÌ TIẾP — "LỖI: sai tham số" là vô dụng,
     "LỖI: hạng mục 'X' không tồn tại, các hạng mục hợp lệ: A, B, C" mới giúp Agent tự sửa.
  4. Tool ghi dữ liệu phải TỰ KIỂM TRA LẠI chính sách — đây là LỚP PHÒNG THỦ SỐ 4,
     lớp duy nhất prompt injection không thể vượt qua vì nó là code Python thuần.

Chạy độc lập:  python src/tools.py
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import bank_api
import database as db
import ocr_client
import rag
import roles

# =============================================================================
# HẰNG SỐ NGHIỆP VỤ (nguồn: data/policies/01_han_muc_chi_tieu.md)
# =============================================================================

# Ngưỡng BẮT BUỘC có người duyệt (Điều 6, QC-TC-01/2026)
HIGH_VALUE_THRESHOLD = 10_000_000

# Hạn mức từng hạng mục trên MỘT hóa đơn (Điều 2, QC-TC-01/2026)
POLICY_LIMITS = {
    "Ăn uống":    {"limit": 1_000_000,  "unit": "mỗi lần",      "approver": "Trưởng phòng"},
    "Đi lại":     {"limit": 3_000_000,  "unit": "mỗi lần",      "approver": "Trưởng phòng"},
    "Khách sạn":  {"limit": 5_000_000,  "unit": "mỗi đêm",      "approver": "Trưởng phòng"},
    "Tiếp khách": {"limit": 5_000_000,  "unit": "mỗi lần",      "approver": "Trưởng phòng"},
    "Thiết bị":   {"limit": 10_000_000, "unit": "mỗi lần",      "approver": "Trưởng phòng"},
    "Thuê ngoài": {"limit": 20_000_000, "unit": "mỗi hợp đồng", "approver": "Giám đốc"},
}

# Ngưỡng bắt buộc chuyển khoản, không được dùng tiền mặt (Điều 4, QD-TC-02/2026)
CASHLESS_THRESHOLD = 20_000_000

# Số ngày tối đa được nộp hóa đơn kể từ ngày lập (Điều 3, QD-TC-02/2026)
MAX_INVOICE_AGE_DAYS = 90

VALID_DEPARTMENTS = ["Tài chính", "Ban Giám đốc", "Marketing", "Sales", "Kỹ thuật", "Hành chính"]


# =============================================================================
# PHIÊN LÀM VIỆC — ai đang gọi tool, và hành động nào đã được người duyệt
# =============================================================================
# Vì sao KHÔNG truyền role/user_id làm tham số của tool?
#   Nếu để `approved_by` là tham số, LLM chỉ cần tự bịa `approved_by="KT-01"` là
#   vượt được cửa duyệt. Đặt ngoài tầm với của LLM là cách duy nhất an toàn.

_SESSION = {"role": roles.NHAN_VIEN, "user_id": "", "approved_actions": set()}


def set_session(role: str, user_id: str = ""):
    """Thiết lập danh tính người đang dùng. Gọi 1 lần khi bắt đầu phiên."""
    _SESSION["role"] = roles.normalize_role(role)
    _SESSION["user_id"] = (user_id or "").strip().upper()
    _SESSION["approved_actions"] = set()


def get_session() -> dict:
    return dict(_SESSION)


def approval_key(invoice_no: str, amount) -> str:
    """Khoá định danh một hành động chuyển khoản cụ thể cần được duyệt."""
    try:
        amt = int(round(float(amount)))
    except (TypeError, ValueError):
        amt = 0
    return f"transfer:{str(invoice_no).strip().upper()}:{amt}"


def grant_approval(key: str):
    """
    Cấp phép cho MỘT hành động cụ thể.
    ⚠️ CHỈ được gọi từ node_human_approval sau khi con người bấm duyệt.
    """
    _SESSION["approved_actions"].add(key)


def is_approved(key: str) -> bool:
    return key in _SESSION["approved_actions"]


# =============================================================================
# HÀM TRỢ GIÚP
# =============================================================================

def _money(amount) -> str:
    return db.format_money(amount)


def _parse_amount(raw) -> float:
    """Đọc số tiền từ chuỗi Agent truyền vào. Không đọc được -> -1 (KHÔNG đoán bừa)."""
    if raw is None:
        return -1.0
    if isinstance(raw, (int, float)):
        return float(raw)
    cleaned = str(raw).lower().replace("đ", "").replace("vnđ", "").replace("vnd", "")
    cleaned = cleaned.replace(" ", "").replace("_", "")
    value = ocr_client.parse_amount(cleaned)
    return value if value > 0 else -1.0


def _match_category(raw: str) -> str:
    """Khớp tên hạng mục người dùng nhập với hạng mục chuẩn (bỏ qua dấu và hoa/thường)."""
    if not raw:
        return ""
    target = rag.strip_accents(str(raw).strip().lower())
    for name in POLICY_LIMITS:
        if rag.strip_accents(name.lower()) == target:
            return name
    for name in POLICY_LIMITS:   # khớp một phần
        if target and target in rag.strip_accents(name.lower()):
            return name
    return ""


def _audit(tool: str, args: str = "", result: str = "", blocked: str = ""):
    """Ghi nhật ký kiểm toán kèm danh tính phiên hiện tại."""
    try:
        db.write_audit(tool=tool, role=_SESSION["role"], user_id=_SESSION["user_id"],
                       args=args, result=result, blocked_reason=blocked)
    except Exception:
        pass    # Ghi log hỏng KHÔNG được làm sập nghiệp vụ chính


# =============================================================================
# TOOL 1 — LIỆT KÊ ẢNH HÓA ĐƠN
# =============================================================================

def list_invoice_files() -> str:
    """Liệt kê các file ảnh hóa đơn đang có trong thư mục data/invoices/."""
    files = ocr_client.list_invoice_images()
    if not files:
        return ("Thư mục data/invoices/ hiện KHÔNG có file ảnh nào. "
                "Hãy yêu cầu người dùng chép ảnh hóa đơn vào thư mục đó trước khi xử lý. "
                "TUYỆT ĐỐI không tự bịa tên file.")
    lines = [f"Tìm thấy {len(files)} ảnh hóa đơn trong data/invoices/:"]
    lines += [f"  {i}. data/invoices/{f}" for i, f in enumerate(files, start=1)]
    return "\n".join(lines)


# =============================================================================
# TOOL 2 — OCR HÓA ĐƠN
# =============================================================================

def ocr_invoice(image_path: str) -> str:
    """Nhận dạng hóa đơn từ ảnh qua service OCR chạy ở máy khác trong mạng LAN."""
    if not image_path or not str(image_path).strip():
        return ("LỖI: Thiếu đường dẫn ảnh. Hãy gọi list_invoice_files trước "
                "để biết những file nào đang có.")

    result = ocr_client.call_ocr(str(image_path).strip())

    if not result.get("ok"):
        _audit("ocr_invoice", args=image_path, result=result.get("error", ""))
        return result.get("error", "LỖI OCR: Lỗi không xác định.")

    lines = [
        f"Đã nhận dạng ảnh {result.get('source_image', image_path)}:",
        f"  - Số hóa đơn   : {result['invoice_no'] or '(KHÔNG ĐỌC ĐƯỢC)'}",
        f"  - Nhà cung cấp : {result['vendor'] or '(KHÔNG ĐỌC ĐƯỢC)'}",
        f"  - Mã số thuế   : {result['tax_code'] or '(KHÔNG ĐỌC ĐƯỢC)'}",
        f"  - Ngày hóa đơn : {result['invoice_date'] or '(KHÔNG ĐỌC ĐƯỢC)'}",
        f"  - Tổng tiền    : {_money(result['amount']) if result['amount'] else '(KHÔNG ĐỌC ĐƯỢC)'}",
        f"  - Thuế GTGT    : {_money(result['vat']) if result['vat'] else '(không có)'}",
    ]

    missing = result.get("missing_fields") or []
    if missing:
        lines.append("")
        lines.append(f"⚠️ CẢNH BÁO: OCR KHÔNG đọc được các trường bắt buộc: {', '.join(missing)}.")
        lines.append("Chiếu theo Điều 6 QD-TC-02/2026, TUYỆT ĐỐI KHÔNG được tự suy đoán hoặc "
                     "điền ước lượng các trường này. Phải yêu cầu người dùng chụp lại ảnh rõ nét "
                     "hoặc nhập tay thông tin còn thiếu.")

    _audit("ocr_invoice", args=image_path, result=f"{result['invoice_no']} / {result['amount']}")
    return "\n".join(lines)


# =============================================================================
# TOOL 3 — TRA CỨU CHÍNH SÁCH (RAG)
# =============================================================================

def search_policy(query: str) -> str:
    """Tra cứu quy chế chi tiêu nội bộ bằng RAG, trả về điều khoản kèm trích dẫn nguồn."""
    if not query or not str(query).strip():
        return ("LỖI: Thiếu từ khoá tra cứu. Ví dụ hợp lệ: "
                "search_policy[\"hạn mức tiếp khách\"]")
    return rag.search_policy_text(str(query).strip(), top_k=3)


# =============================================================================
# TOOL 4 — ĐỐI CHIẾU TUÂN THỦ CHÍNH SÁCH
# =============================================================================

def check_policy_compliance(category: str, amount, tax_code: str = "",
                            invoice_date: str = "") -> str:
    """Đối chiếu một khoản chi với hạn mức và điều kiện hóa đơn, trả về PASS/FAIL."""
    cat = _match_category(category)
    if not cat:
        return (f"LỖI: Hạng mục '{category}' không có trong quy chế. "
                f"Các hạng mục hợp lệ: {', '.join(POLICY_LIMITS)}.")

    amt = _parse_amount(amount)
    if amt < 0:
        return (f"LỖI: Không đọc được số tiền '{amount}'. "
                f"Hãy truyền số thuần, ví dụ: check_policy_compliance[\"{cat}\", \"4500000\"]")

    rule = POLICY_LIMITS[cat]
    violations, warnings = [], []

    # --- Kiểm tra hạn mức hạng mục ---
    if amt > rule["limit"]:
        violations.append(
            f"Vượt hạn mức hạng mục '{cat}': {_money(amt)} > {_money(rule['limit'])} "
            f"({rule['unit']}) — Điều 2, QC-TC-01/2026. Cần nâng lên cấp duyệt "
            f"cao hơn {rule['approver']}."
        )

    # --- Kiểm tra ngưỡng bắt buộc người duyệt ---
    if amt >= HIGH_VALUE_THRESHOLD:
        warnings.append(
            f"Số tiền {_money(amt)} ĐẠT/VƯỢT ngưỡng {_money(HIGH_VALUE_THRESHOLD)} nên "
            f"BẮT BUỘC phải có xác nhận trực tiếp của người có thẩm quyền trước khi "
            f"chuyển khoản (Điều 6, QC-TC-01/2026). Hệ thống KHÔNG được tự động chi."
        )

    # --- Kiểm tra mã số thuế ---
    tax_code = str(tax_code or "").strip().replace(" ", "").replace("-", "")
    if not tax_code:
        violations.append("Thiếu mã số thuế nhà cung cấp — Điều 2, QD-TC-02/2026. "
                          "Hóa đơn không có MST bị từ chối thanh toán.")
    elif not (tax_code.isdigit() and len(tax_code) in (10, 13)):
        violations.append(f"Mã số thuế '{tax_code}' sai định dạng — phải là 10 số "
                          f"(doanh nghiệp) hoặc 13 số (đơn vị trực thuộc).")

    # --- Kiểm tra thời hạn nộp hóa đơn ---
    if invoice_date:
        from datetime import date, datetime as _dt
        try:
            inv_date = _dt.strptime(str(invoice_date).strip(), "%Y-%m-%d").date()
            age = (date.today() - inv_date).days
            if age > MAX_INVOICE_AGE_DAYS:
                violations.append(f"Hóa đơn lập ngày {invoice_date}, đã {age} ngày — quá "
                                  f"{MAX_INVOICE_AGE_DAYS} ngày nên KHÔNG được thanh toán "
                                  f"trong mọi trường hợp (Điều 3, QD-TC-02/2026).")
            elif age > 30:
                warnings.append(f"Hóa đơn đã {age} ngày (quá 30 ngày) — cần giải trình "
                                f"bằng văn bản của Trưởng phòng (Điều 3, QD-TC-02/2026).")
            elif age < 0:
                violations.append(f"Ngày hóa đơn {invoice_date} nằm ở TƯƠNG LAI — dữ liệu "
                                  f"không hợp lệ, cần kiểm tra lại.")
        except ValueError:
            warnings.append(f"Không đọc được ngày '{invoice_date}' (cần dạng YYYY-MM-DD), "
                            f"bỏ qua kiểm tra thời hạn.")

    # --- Ngưỡng bắt buộc chuyển khoản ---
    if amt >= CASHLESS_THRESHOLD:
        warnings.append(f"Hóa đơn từ {_money(CASHLESS_THRESHOLD)} trở lên BẮT BUỘC thanh toán "
                        f"qua chuyển khoản mới được khấu trừ thuế (Điều 4, QD-TC-02/2026).")

    # --- Kết luận ---
    verdict = "FAIL" if violations else "PASS"
    out = [f"KẾT QUẢ ĐỐI CHIẾU: {verdict}",
           f"  Hạng mục   : {cat}",
           f"  Số tiền    : {_money(amt)}",
           f"  Hạn mức    : {_money(rule['limit'])} {rule['unit']}",
           f"  Cấp duyệt  : {rule['approver']}"]

    if violations:
        out.append("\n❌ VI PHẠM (không được thanh toán tự động):")
        out += [f"  {i}. {v}" for i, v in enumerate(violations, 1)]
    if warnings:
        out.append("\n⚠️ LƯU Ý:")
        out += [f"  {i}. {w}" for i, w in enumerate(warnings, 1)]
    if verdict == "PASS" and not warnings:
        out.append("\n✅ Khoản chi nằm trong hạn mức và đủ điều kiện hóa đơn.")

    return "\n".join(out)


# =============================================================================
# TOOL 5 — KIỂM TRA TRÙNG LẶP
# =============================================================================

def check_duplicate_invoice(invoice_no: str, tax_code: str = "",
                            vendor: str = "", amount="") -> str:
    """Kiểm tra hóa đơn đã được thanh toán trước đó chưa (Điều 5, QD-TC-02/2026)."""
    invoice_no = str(invoice_no or "").strip()
    if not invoice_no and not vendor:
        return ("LỖI: Cần ít nhất số hóa đơn hoặc tên nhà cung cấp để kiểm tra trùng. "
                "Ví dụ: check_duplicate_invoice[\"HD-2026-0013\", \"0101245789\"]")

    amt = _parse_amount(amount) if amount else 0
    matches = db.find_duplicate_invoice(
        invoice_no=invoice_no,
        tax_code=str(tax_code or "").strip(),
        vendor=str(vendor or "").strip(),
        amount=amt if amt > 0 else 0,
    )

    paid = db.is_already_paid(invoice_no) if invoice_no else {}

    # ⚠️ PHÂN BIỆT QUAN TRỌNG:
    #   "Đã có HỒ SƠ hóa đơn trong hệ thống" ≠ "Đã THANH TOÁN hóa đơn đó".
    # Hóa đơn ở trạng thái NEW/PENDING chính là hồ sơ của lần xử lý hiện tại,
    # báo nó là trùng sẽ chặn oan nghiệp vụ hợp lệ.
    self_records = [m for m in matches if m["match_type"] == "EXACT"]
    similar = [m for m in matches if m["match_type"] == "SIMILAR"]
    paid_self = [m for m in self_records if m["status"] == db.STATUS_PAID]

    # ---- Trường hợp 1: đã thực sự chi tiền ----
    if paid or paid_self:
        out = [f"❌ TRÙNG LẶP — HÓA ĐƠN '{invoice_no}' ĐÃ ĐƯỢC THANH TOÁN. KHÔNG ĐƯỢC CHI LẠI."]
        if paid:
            out.append(f"  - Mã giao dịch   : {paid['transaction_id']}")
            out.append(f"  - Số tiền        : {_money(paid['amount'])}")
            out.append(f"  - Thời điểm chi  : {paid['paid_at']}")
            out.append(f"  - Người thực hiện: {paid['paid_by'] or 'không rõ'}")
        for m in paid_self:
            out.append(f"  - Trạng thái hồ sơ: {m['status']} ({m['vendor']}, "
                       f"{_money(m['amount'])}, {m['invoice_date']})")
        out.append("\nChiếu Điều 5 QD-TC-02/2026: mỗi hóa đơn chỉ được thanh toán MỘT lần. "
                   "PHẢI TỪ CHỐI đề nghị này và thông báo cho người nộp.")
        return "\n".join(out)

    out = []

    # ---- Trường hợp 2: có hồ sơ nhưng CHƯA chi ----
    if self_records:
        m = self_records[0]
        out.append(f"✅ KHÔNG TRÙNG LẶP THANH TOÁN.")
        out.append(f"Hóa đơn '{m['invoice_no']}' đã có hồ sơ trong hệ thống ở trạng thái "
                   f"{m['status']} nhưng CHƯA phát sinh giao dịch chi tiền nào.")
        out.append(f"  - Nhà cung cấp: {m['vendor']}")
        out.append(f"  - Số tiền     : {_money(m['amount'])}")
        out.append(f"  - Ngày        : {m['invoice_date']}")
        out.append("Đây chính là hồ sơ của lần xử lý hiện tại, KHÔNG phải trùng lặp. "
                   "Được phép tiếp tục các bước tiếp theo.")
    else:
        out.append(f"✅ KHÔNG TRÙNG LẶP. Hóa đơn '{invoice_no or vendor}' chưa từng xuất hiện "
                   f"trong sổ cái. Được phép tiếp tục các bước tiếp theo.")

    # ---- Trường hợp 3: nghi vấn trùng với hóa đơn KHÁC ----
    if similar:
        out.append(f"\n⚠️ LƯU Ý — có {len(similar)} hóa đơn KHÁC cùng nhà cung cấp và "
                   f"cùng số tiền, cần người xem lại để loại trừ khả năng khai trùng:")
        for m in similar:
            out.append(f"  - {m['invoice_no']} | {m['vendor']} | {_money(m['amount'])} "
                       f"| {m['invoice_date']} | {m['status']}")

    return "\n".join(out)


# =============================================================================
# TOOL 6 — TRUY VẤN SỔ CÁI (chỉ Kế toán)
# =============================================================================

def query_finance_db(filter_type: str, value: str = "") -> str:
    """Truy vấn sổ cái hóa đơn toàn công ty theo bộ lọc định sẵn."""
    filter_type = str(filter_type or "").strip().lower()

    if filter_type not in db.VALID_FILTERS:
        lines = [f"LỖI: Bộ lọc '{filter_type}' không hợp lệ. Các bộ lọc được hỗ trợ:"]
        lines += [f"  - {k}: {v}" for k, v in db.VALID_FILTERS.items()]
        lines.append('Ví dụ: query_finance_db["status", "PENDING"]')
        return "\n".join(lines)

    rows = db.query_invoices(filter_type, str(value or "").strip())
    if not rows:
        return f"Không có hóa đơn nào khớp bộ lọc {filter_type}='{value}'."

    total = sum(r["amount"] for r in rows)
    out = [f"Tìm thấy {len(rows)} hóa đơn (tổng {_money(total)}) với {filter_type}='{value}':",
           ""]
    for r in rows[:20]:
        out.append(f"  {r['invoice_no']} | {r['vendor'][:28]:<28} | {r['category']:<10} | "
                   f"{_money(r['amount']):>14} | {r['invoice_date']} | {r['status']}")
    if len(rows) > 20:
        out.append(f"  ... và {len(rows) - 20} hóa đơn khác (đã giới hạn hiển thị 20 dòng).")
    return "\n".join(out)


# =============================================================================
# TOOL 7 — TRA TRẠNG THÁI HÓA ĐƠN CỦA CHÍNH MÌNH
# =============================================================================

def get_my_invoice_status(user_id: str = "") -> str:
    """Tra danh sách và trạng thái hóa đơn do chính người dùng hiện tại nộp."""
    session_user = _SESSION.get("user_id", "")
    role = _SESSION.get("role")

    requested = str(user_id or "").strip().upper()

    # Nhân viên KHÔNG được xem hóa đơn của người khác (Điều 4, QD-TC-04/2026)
    if role == roles.NHAN_VIEN and requested and requested != session_user:
        _audit("get_my_invoice_status", args=requested,
               blocked=f"Nhân viên {session_user} cố xem hóa đơn của {requested}")
        return (f"LỖI PHÂN QUYỀN: Vai trò Nhân viên chỉ được xem hóa đơn của CHÍNH MÌNH "
                f"({session_user}), không được xem của '{requested}' "
                f"(Điều 4, QD-TC-04/2026). Sự việc đã ghi vào nhật ký kiểm toán.")

    target = requested or session_user
    if not target:
        return ("LỖI: Chưa xác định được người dùng. Hãy đăng nhập hoặc truyền mã nhân viên, "
                "ví dụ: get_my_invoice_status[\"EMP-01\"]")

    rows = db.list_invoices_by_user(target)
    if not rows:
        return f"Nhân viên {target} chưa nộp hóa đơn nào."

    out = [f"Hóa đơn do {target} nộp ({len(rows)} hóa đơn):", ""]
    for r in rows:
        out.append(f"  {r['invoice_no']} | {r['vendor'][:28]:<28} | {r['category']:<10} | "
                   f"{_money(r['amount']):>14} | {r['invoice_date']} | {r['status']}")

    by_status = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    out.append("")
    out.append("Tổng hợp: " + ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())))
    return "\n".join(out)


# =============================================================================
# TOOL 8 — LẬP PHIẾU ĐỀ NGHỊ DUYỆT CHI  ✍️ GHI DỮ LIỆU
# =============================================================================

def create_payment_ticket(invoice_no: str, amount, target_department: str,
                          reason: str = "") -> str:
    """Lập phiếu đề nghị duyệt chi gửi phòng ban có thẩm quyền (Điều 4, QT-TC-03/2026)."""
    invoice_no = str(invoice_no or "").strip()
    if not invoice_no:
        return "LỖI: Thiếu số hóa đơn. Ví dụ: create_payment_ticket[\"HD-2026-0016\", \"12800000\", \"Tài chính\", \"Vượt ngưỡng duyệt tự động\"]"

    amt = _parse_amount(amount)
    if amt < 0:
        return f"LỖI: Không đọc được số tiền '{amount}'. Hãy truyền số thuần, ví dụ '12800000'."

    dept = str(target_department or "").strip()
    if not dept:
        return (f"LỖI: Thiếu phòng ban tiếp nhận. Các phòng ban hợp lệ: "
                f"{', '.join(VALID_DEPARTMENTS)}.")

    matched_dept = next(
        (d for d in VALID_DEPARTMENTS
         if rag.strip_accents(d.lower()) == rag.strip_accents(dept.lower())), ""
    )
    if not matched_dept:
        return (f"LỖI: Phòng ban '{dept}' không tồn tại. Các phòng ban hợp lệ: "
                f"{', '.join(VALID_DEPARTMENTS)}.")

    if not str(reason or "").strip():
        return ("LỖI: Thiếu lý do đề nghị. Phiếu đề nghị phải nêu rõ vì sao khoản chi này "
                "cần chuyển lên cấp duyệt (Điều 4, QT-TC-03/2026).")

    invoice = db.get_invoice_by_no(invoice_no)
    vendor = invoice.get("vendor", "")

    ticket_id = db.create_ticket(
        invoice_no=invoice_no, vendor=vendor, amount=amt,
        requested_by=_SESSION.get("user_id") or "(không rõ)",
        target_department=matched_dept, reason=str(reason).strip(),
    )
    if invoice:
        db.update_invoice_status(invoice_no, db.STATUS_PENDING)

    _audit("create_payment_ticket", args=f"{invoice_no}|{amt}|{matched_dept}",
           result=f"ticket #{ticket_id}")

    return (f"✅ Đã lập phiếu đề nghị #{ticket_id}.\n"
            f"  - Hóa đơn      : {invoice_no}"
            + (f" ({vendor})" if vendor else "") + "\n"
            f"  - Số tiền      : {_money(amt)}\n"
            f"  - Gửi tới      : {matched_dept}\n"
            f"  - Người đề nghị: {_SESSION.get('user_id') or '(không rõ)'}\n"
            f"  - Trạng thái   : OPEN (đang chờ duyệt)\n"
            f"Hóa đơn đã chuyển sang trạng thái PENDING. "
            f"KHÔNG được chuyển khoản cho tới khi phiếu này được phê duyệt.")


# =============================================================================
# TOOL 9 — PHÊ DUYỆT PHIẾU ĐỀ NGHỊ  ✍️ GHI DỮ LIỆU (chỉ Kế toán)
# =============================================================================

def approve_ticket(ticket_id, decision: str, note: str = "") -> str:
    """Phê duyệt hoặc từ chối một phiếu đề nghị duyệt chi."""
    try:
        tid = int(str(ticket_id).strip().lstrip("#"))
    except (TypeError, ValueError):
        return f"LỖI: Mã phiếu '{ticket_id}' không hợp lệ, phải là số nguyên (ví dụ: 1)."

    decision = str(decision or "").strip().upper()
    aliases = {"APPROVE": db.TICKET_APPROVED, "APPROVED": db.TICKET_APPROVED,
               "DUYET": db.TICKET_APPROVED, "REJECT": db.TICKET_REJECTED,
               "REJECTED": db.TICKET_REJECTED, "TU_CHOI": db.TICKET_REJECTED}
    status = aliases.get(decision, "")
    if not status:
        return (f"LỖI: Quyết định '{decision}' không hợp lệ. "
                f"Chỉ chấp nhận 'APPROVE' hoặc 'REJECT'.")

    ticket = db.get_ticket(tid)
    if not ticket:
        open_tickets = db.list_tickets(status=db.TICKET_OPEN)
        ids = ", ".join(f"#{t['id']}" for t in open_tickets) or "(không có phiếu nào đang mở)"
        return f"LỖI: Không tìm thấy phiếu #{tid}. Các phiếu đang mở: {ids}."

    if ticket["status"] != db.TICKET_OPEN:
        return (f"LỖI: Phiếu #{tid} đã ở trạng thái {ticket['status']} "
                f"(xử lý bởi {ticket.get('resolved_by') or 'không rõ'} lúc "
                f"{ticket.get('resolved_at') or 'không rõ'}), không thể xử lý lại.")

    ok = db.resolve_ticket(tid, status, _SESSION.get("user_id") or "(không rõ)",
                           str(note or "").strip())
    if not ok:
        return f"LỖI: Không cập nhật được phiếu #{tid}."

    if status == db.TICKET_REJECTED and ticket.get("invoice_no"):
        db.update_invoice_status(ticket["invoice_no"], db.STATUS_REJECTED)

    _audit("approve_ticket", args=f"#{tid}|{status}", result=status)

    verdict = "PHÊ DUYỆT" if status == db.TICKET_APPROVED else "TỪ CHỐI"
    extra = ("Hóa đơn có thể tiến hành chuyển khoản (vẫn phải qua bước xác nhận của "
             "người có thẩm quyền nếu số tiền đạt ngưỡng)."
             if status == db.TICKET_APPROVED else
             "Hóa đơn đã chuyển sang trạng thái REJECTED.")
    return (f"✅ Đã {verdict} phiếu #{tid}.\n"
            f"  - Hóa đơn : {ticket.get('invoice_no') or '(không gắn hóa đơn)'}\n"
            f"  - Số tiền : {_money(ticket.get('amount') or 0)}\n"
            f"  - Ghi chú : {note or '(không có)'}\n"
            f"{extra}")


# =============================================================================
# TOOL 10 — CHUYỂN KHOẢN  💰 GHI DỮ LIỆU + TIÊU TIỀN THẬT
# =============================================================================

def transfer_payment(invoice_no: str, vendor: str, amount, category: str,
                     invoice_date: str = "", tax_code: str = "",
                     account_no: str = "") -> str:
    """
    Thực hiện lệnh chuyển khoản thanh toán hóa đơn.

    ⚠️ ĐÂY LÀ LỚP PHÒNG THỦ SỐ 4 — lớp CUỐI CÙNG và DUY NHẤT mà prompt injection
    không thể vượt qua, vì toàn bộ kiểm tra dưới đây là code Python thuần,
    không phụ thuộc vào việc LLM có "nghe lời" system prompt hay không.
    """
    invoice_no = str(invoice_no or "").strip()
    args_repr = f"{invoice_no}|{vendor}|{amount}|{category}"

    # ---- Chốt chặn 0: đủ tham số ----
    if not invoice_no or not str(vendor or "").strip():
        return ("LỖI: Thiếu số hóa đơn hoặc tên nhà cung cấp. Phải chạy ocr_invoice "
                "để lấy thông tin thật trước, TUYỆT ĐỐI không tự bịa.")

    amt = _parse_amount(amount)
    if amt <= 0:
        _audit("transfer_payment", args_repr, blocked="Số tiền không hợp lệ")
        return (f"LỖI: Số tiền '{amount}' không hợp lệ. Không được đoán số tiền — "
                f"phải lấy từ kết quả OCR hoặc do người dùng nhập rõ ràng.")

    cat = _match_category(category)
    if not cat:
        _audit("transfer_payment", args_repr, blocked=f"Hạng mục sai: {category}")
        return (f"LỖI: Hạng mục '{category}' không hợp lệ. "
                f"Các hạng mục: {', '.join(POLICY_LIMITS)}.")

    # ---- Chốt chặn 1: hóa đơn đã thanh toán chưa? (Điều 5, QD-TC-02) ----
    paid = db.is_already_paid(invoice_no)
    if paid:
        _audit("transfer_payment", args_repr,
               blocked=f"Hóa đơn đã thanh toán, giao dịch {paid['transaction_id']}")
        return (f"❌ TỪ CHỐI CHUYỂN KHOẢN: Hóa đơn '{invoice_no}' ĐÃ ĐƯỢC THANH TOÁN "
                f"ngày {paid['paid_at']} với mã giao dịch {paid['transaction_id']} "
                f"({_money(paid['amount'])}).\n"
                f"Điều 5 QD-TC-02/2026: mỗi hóa đơn chỉ được thanh toán MỘT lần. "
                f"Sự việc đã ghi vào nhật ký kiểm toán.")

    # ---- Chốt chặn 2: mã số thuế (Điều 2, QD-TC-02) ----
    tax_code = str(tax_code or "").strip().replace(" ", "").replace("-", "")
    if not tax_code or not (tax_code.isdigit() and len(tax_code) in (10, 13)):
        _audit("transfer_payment", args_repr, blocked=f"MST không hợp lệ: '{tax_code}'")
        return (f"❌ TỪ CHỐI CHUYỂN KHOẢN: Mã số thuế nhà cung cấp "
                f"{'bị thiếu' if not tax_code else f'''\"{tax_code}\" sai định dạng'''}. "
                f"Điều 2 QD-TC-02/2026 yêu cầu MST 10 số (doanh nghiệp) hoặc 13 số "
                f"(đơn vị trực thuộc). Hóa đơn không có MST hợp lệ bị từ chối thanh toán.")

    # ---- Chốt chặn 3: hạn mức hạng mục (Điều 2, QC-TC-01) ----
    rule = POLICY_LIMITS[cat]
    if amt > rule["limit"]:
        _audit("transfer_payment", args_repr,
               blocked=f"Vượt hạn mức {cat}: {amt} > {rule['limit']}")
        return (f"❌ TỪ CHỐI CHUYỂN KHOẢN: Số tiền {_money(amt)} VƯỢT hạn mức hạng mục "
                f"'{cat}' là {_money(rule['limit'])} ({rule['unit']}) — Điều 2 QC-TC-01/2026.\n"
                f"Bắt buộc phải lập phiếu đề nghị gửi cấp {rule['approver']} bằng tool "
                f"create_payment_ticket. Sự việc đã ghi vào nhật ký kiểm toán.")

    # ---- Chốt chặn 4: ngưỡng bắt buộc người duyệt (Điều 6, QC-TC-01) ----
    # LLM KHÔNG THỂ tự cấp phép — cờ duyệt nằm ngoài tham số tool.
    if amt >= HIGH_VALUE_THRESHOLD and not is_approved(approval_key(invoice_no, amt)):
        _audit("transfer_payment", args_repr,
               blocked=f"Chưa có phê duyệt của người có thẩm quyền cho {amt}")
        return (f"⛔ TẠM DỪNG — CHỜ NGƯỜI DUYỆT: Số tiền {_money(amt)} đạt/vượt ngưỡng "
                f"{_money(HIGH_VALUE_THRESHOLD)} nên BẮT BUỘC phải có xác nhận trực tiếp của "
                f"người có thẩm quyền (Điều 6 QC-TC-01/2026 và nguyên tắc bốn mắt tại "
                f"Điều 6 QT-TC-03/2026).\n"
                f"Hệ thống tự động KHÔNG được phép thực hiện giao dịch này. "
                f"Hãy lập phiếu đề nghị bằng create_payment_ticket hoặc chờ người duyệt "
                f"xác nhận trên giao diện.")

    # ---- Qua hết chốt chặn: gọi cổng thanh toán ----
    account_no = str(account_no or "").strip() or bank_api.lookup_account(vendor)
    result = bank_api.transfer(account_no=account_no, amount=amt,
                               content=f"Thanh toan {invoice_no}", vendor=vendor)

    invoice = db.get_invoice_by_no(invoice_no)
    inv_date = str(invoice_date or "").strip() or invoice.get("invoice_date", "")

    # Ghi nhận giao dịch kể cả khi thất bại — để có dấu vết kiểm toán
    db.insert_payment(
        invoice_no=invoice_no, vendor=vendor, category=cat, amount=amt,
        invoice_date=inv_date or "1970-01-01",
        transaction_id=result.get("transaction_id") or "",
        bank_status=result["status"],
        paid_by=_SESSION.get("user_id") or "",
        note=result["message"][:200],
        invoice_id=invoice.get("id"),
    )

    if result["ok"]:
        if invoice:
            db.update_invoice_status(invoice_no, db.STATUS_PAID)
        _audit("transfer_payment", args_repr, result=result["transaction_id"])
        return (f"✅ CHUYỂN KHOẢN THÀNH CÔNG\n"
                f"  - Hóa đơn      : {invoice_no}\n"
                f"  - Nhà cung cấp : {vendor}\n"
                f"  - Số tài khoản : {account_no}\n"
                f"  - Số tiền      : {_money(amt)}\n"
                f"  - Mã giao dịch : {result['transaction_id']}\n"
                f"  - Số dư còn lại: {_money(result['balance_after'])}\n"
                f"Hóa đơn đã chuyển sang trạng thái PAID.")

    _audit("transfer_payment", args_repr, result=result["status"],
           blocked=f"Ngân hàng từ chối: {result['status']}")
    return (f"❌ GIAO DỊCH THẤT BẠI — mã lỗi {result['status']}\n"
            f"  {result['message']}\n"
            f"Điều 5 QT-TC-03/2026: TUYỆT ĐỐI KHÔNG gửi lại lệnh chuyển khoản một cách "
            f"máy móc vì có thể chuyển tiền hai lần. Hãy báo lại người dùng hoặc lập "
            f"phiếu đề nghị nếu nguyên nhân là thiếu số dư.")


# =============================================================================
# TOOL 11 — BÁO CÁO TÀI CHÍNH (chỉ Kế toán)
# =============================================================================

def generate_business_report(period_type: str, period_value: str) -> str:
    """Kết xuất báo cáo chi phí theo tuần / tháng / quý từ các giao dịch đã thanh toán."""
    rep = db.financial_report(str(period_type or "").strip(), str(period_value or "").strip())

    if not rep["ok"]:
        return (f"LỖI: {rep['error']}\n"
                f"Ví dụ hợp lệ:\n"
                f"  generate_business_report[\"week\", \"2026-W30\"]\n"
                f"  generate_business_report[\"month\", \"2026-07\"]\n"
                f"  generate_business_report[\"quarter\", \"2026-Q3\"]")

    if rep["count"] == 0:
        return (f"BÁO CÁO {rep['period_label'].upper()}: Không có giao dịch thanh toán "
                f"nào trong kỳ này.")

    out = [f"📊 BÁO CÁO CHI PHÍ — {rep['period_label'].upper()}",
           f"  Tổng chi phí : {_money(rep['total'])}",
           f"  Số hóa đơn   : {rep['count']}",
           f"  Trung bình   : {_money(rep['total'] / rep['count'])}/hóa đơn",
           "",
           "  PHÂN BỔ THEO HẠNG MỤC:"]

    for c in rep["by_category"]:
        pct = c["total"] / rep["total"] * 100 if rep["total"] else 0
        bar = "█" * max(1, int(pct / 5))
        out.append(f"    {c['category']:<12} {_money(c['total']):>15}  "
                   f"({c['cnt']} HĐ, {pct:4.1f}%) {bar}")

    out.append("")
    out.append("  TOP NHÀ CUNG CẤP:")
    for i, v in enumerate(rep["top_vendors"], 1):
        out.append(f"    {i}. {v['vendor'][:32]:<32} {_money(v['total']):>15} ({v['cnt']} HĐ)")

    return "\n".join(out)


# =============================================================================
# TOOL SPECS — hợp đồng công cụ (8 câu hỏi chuẩn của CODELAB)
# =============================================================================
# Dùng để SINH TỰ ĐỘNG danh sách tool trong System Prompt ➔ prompt không bao giờ
# lệch với code. Sửa tool ở đây, prompt tự cập nhật theo.

TOOL_SPECS = {
    "list_invoice_files": {
        "description": "Liệt kê các file ảnh hóa đơn đang có trong thư mục data/invoices/.",
        "when_to_use": "Khi người dùng nói chung chung 'xử lý hóa đơn' mà chưa nêu rõ tên file, hoặc khi cần kiểm tra file có tồn tại không.",
        "args": [],
        "returns": "Danh sách đường dẫn file, hoặc thông báo thư mục trống.",
        "side_effect": "Không (chỉ đọc)",
        "example": 'list_invoice_files[]',
        "errors": "Thư mục trống -> trả thông báo hướng dẫn, KHÔNG được tự bịa tên file.",
    },
    "ocr_invoice": {
        "description": "Nhận dạng hóa đơn từ ảnh, trích ra số hóa đơn, nhà cung cấp, mã số thuế, ngày và tổng tiền.",
        "when_to_use": "BẮT BUỘC gọi đầu tiên trước mọi thao tác xử lý hóa đơn. Không có kết quả OCR thì không được suy đoán bất kỳ con số nào.",
        "args": [("image_path", "str", "Đường dẫn ảnh, ví dụ 'data/invoices/hd_001.jpg'")],
        "returns": "6 trường thông tin hóa đơn, kèm cảnh báo nếu có trường không đọc được.",
        "side_effect": "Không (chỉ đọc, có gọi HTTP tới service OCR ngoài mạng)",
        "example": 'ocr_invoice["data/invoices/hd_001.jpg"]',
        "errors": "File không tồn tại / service OCR chết / timeout -> trả 'LỖI OCR: ...'. Khi đó PHẢI báo người dùng, KHÔNG được đoán số liệu.",
    },
    "search_policy": {
        "description": "Tra cứu quy chế chi tiêu nội bộ của công ty, trả về điều khoản liên quan kèm trích dẫn nguồn.",
        "when_to_use": "Khi cần biết hạn mức, điều kiện hóa đơn, ngưỡng phê duyệt hoặc quy định phân quyền. Luôn gọi trước khi kết luận một khoản chi có hợp lệ hay không.",
        "args": [("query", "str", "Từ khoá hoặc câu hỏi, ví dụ 'hạn mức tiếp khách'")],
        "returns": "Tối đa 3 điều khoản liên quan nhất, mỗi điều kèm [Nguồn: file § Điều].",
        "side_effect": "Không (chỉ đọc)",
        "example": 'search_policy["hạn mức tiếp khách"]',
        "errors": "Không tìm thấy -> trả gợi ý từ khoá khác. KHÔNG tự bịa nội dung điều khoản.",
    },
    "check_policy_compliance": {
        "description": "Đối chiếu một khoản chi với hạn mức hạng mục, mã số thuế và thời hạn nộp, trả về PASS hoặc FAIL kèm lý do.",
        "when_to_use": "Sau khi đã có dữ liệu thật từ ocr_invoice. Bắt buộc gọi trước khi chuyển khoản.",
        "args": [("category", "str", "Một trong: " + ", ".join(POLICY_LIMITS)),
                 ("amount", "str", "Số tiền dạng số thuần, ví dụ '4500000'"),
                 ("tax_code", "str", "Mã số thuế nhà cung cấp (10 hoặc 13 số)"),
                 ("invoice_date", "str", "Ngày hóa đơn dạng YYYY-MM-DD")],
        "returns": "PASS/FAIL kèm danh sách vi phạm và lưu ý, có trích dẫn điều khoản.",
        "side_effect": "Không (chỉ đọc)",
        "example": 'check_policy_compliance["Tiếp khách", "4750000", "0101245789", "2026-07-02"]',
        "errors": "Hạng mục sai -> liệt kê hạng mục hợp lệ. Số tiền không đọc được -> báo lỗi, KHÔNG đoán.",
    },
    "check_duplicate_invoice": {
        "description": "Kiểm tra hóa đơn đã từng được thanh toán hoặc có bản ghi nghi trùng trong sổ cái chưa.",
        "when_to_use": "BẮT BUỘC gọi trước mỗi lần chuyển khoản, để tránh thanh toán hai lần cho cùng một hóa đơn.",
        "args": [("invoice_no", "str", "Số hóa đơn, ví dụ 'HD-2026-0013'"),
                 ("tax_code", "str", "Mã số thuế nhà cung cấp"),
                 ("vendor", "str", "Tên nhà cung cấp"),
                 ("amount", "str", "Số tiền")],
        "returns": "Kết luận có trùng hay không, kèm chi tiết giao dịch cũ nếu có.",
        "side_effect": "Không (chỉ đọc)",
        "example": 'check_duplicate_invoice["HD-2026-0013", "0101245789"]',
        "errors": "Thiếu cả số hóa đơn lẫn tên nhà cung cấp -> báo lỗi thiếu tham số.",
    },
    "query_finance_db": {
        "description": "Truy vấn sổ cái hóa đơn toàn công ty theo bộ lọc định sẵn.",
        "when_to_use": "Khi cần tra cứu hóa đơn theo nhà cung cấp, hạng mục, trạng thái, phòng ban hoặc khoảng ngày.",
        "args": [("filter_type", "str", "Một trong: " + ", ".join(db.VALID_FILTERS)),
                 ("value", "str", "Giá trị lọc tương ứng")],
        "returns": "Danh sách hóa đơn khớp bộ lọc kèm tổng tiền.",
        "side_effect": "Không (chỉ đọc)",
        "example": 'query_finance_db["status", "PENDING"]',
        "errors": "Bộ lọc không hợp lệ -> liệt kê các bộ lọc được hỗ trợ.",
    },
    "get_my_invoice_status": {
        "description": "Xem danh sách và trạng thái các hóa đơn do chính người dùng hiện tại nộp.",
        "when_to_use": "Khi người dùng hỏi 'hóa đơn của tôi đến đâu rồi', 'đã được duyệt chưa'.",
        "args": [("user_id", "str", "Mã nhân viên (bỏ trống = người đang đăng nhập)")],
        "returns": "Danh sách hóa đơn kèm trạng thái NEW/PENDING/PAID/REJECTED.",
        "side_effect": "Không (chỉ đọc)",
        "example": 'get_my_invoice_status["EMP-01"]',
        "errors": "Nhân viên xem hóa đơn người khác -> bị từ chối và ghi nhật ký kiểm toán.",
    },
    "create_payment_ticket": {
        "description": "Lập phiếu đề nghị duyệt chi gửi phòng ban có thẩm quyền.",
        "when_to_use": "Khi khoản chi vượt hạn mức, vượt ngưỡng duyệt tự động, thuộc thẩm quyền phòng khác, hoặc ngân sách đã cạn. Đây là đường đi ĐÚNG khi không được phép chuyển khoản trực tiếp.",
        "args": [("invoice_no", "str", "Số hóa đơn"),
                 ("amount", "str", "Số tiền"),
                 ("target_department", "str", "Một trong: " + ", ".join(VALID_DEPARTMENTS)),
                 ("reason", "str", "Lý do cần chuyển lên cấp duyệt")],
        "returns": "Mã phiếu vừa lập và trạng thái OPEN.",
        "side_effect": "✍️ CÓ — ghi bản ghi mới vào bảng tickets, đổi trạng thái hóa đơn sang PENDING.",
        "example": 'create_payment_ticket["HD-2026-0016", "12800000", "Tài chính", "Vượt ngưỡng duyệt tự động 10 triệu"]',
        "errors": "Thiếu lý do hoặc phòng ban sai -> báo lỗi kèm danh sách hợp lệ.",
    },
    "approve_ticket": {
        "description": "Phê duyệt hoặc từ chối một phiếu đề nghị duyệt chi đang mở.",
        "when_to_use": "Khi kế toán xử lý hàng đợi phiếu đề nghị.",
        "args": [("ticket_id", "str", "Mã phiếu, ví dụ '1'"),
                 ("decision", "str", "'APPROVE' hoặc 'REJECT'"),
                 ("note", "str", "Ghi chú xử lý")],
        "returns": "Xác nhận đã duyệt/từ chối kèm thông tin phiếu.",
        "side_effect": "✍️ CÓ — đổi trạng thái phiếu và có thể đổi trạng thái hóa đơn.",
        "example": 'approve_ticket["1", "APPROVE", "Đã kiểm tra hồ sơ đầy đủ"]',
        "errors": "Phiếu không tồn tại hoặc đã xử lý -> báo lỗi, không xử lý lại.",
    },
    "transfer_payment": {
        "description": "Thực hiện lệnh chuyển khoản thanh toán hóa đơn cho nhà cung cấp.",
        "when_to_use": "CHỈ gọi sau khi đã có ĐỦ bằng chứng: kết quả ocr_invoice, kết quả check_policy_compliance là PASS, và check_duplicate_invoice xác nhận không trùng.",
        "args": [("invoice_no", "str", "Số hóa đơn"),
                 ("vendor", "str", "Tên nhà cung cấp"),
                 ("amount", "str", "Số tiền"),
                 ("category", "str", "Hạng mục chi phí"),
                 ("invoice_date", "str", "Ngày hóa đơn YYYY-MM-DD"),
                 ("tax_code", "str", "Mã số thuế nhà cung cấp")],
        "returns": "Mã giao dịch và số dư còn lại nếu thành công, hoặc lý do bị từ chối.",
        "side_effect": "💰 CÓ — TIÊU TIỀN THẬT, ghi bảng payments, đổi trạng thái hóa đơn sang PAID. KHÔNG THỂ HOÀN TÁC.",
        "example": 'transfer_payment["HD-2026-0017", "Nhà hàng Quán Ăn Ngon", "7200000", "Tiếp khách", "2026-07-22", "0104433221"]',
        "errors": ("Tool TỰ TỪ CHỐI nếu: hóa đơn đã thanh toán, thiếu/sai mã số thuế, "
                   "vượt hạn mức hạng mục, hoặc số tiền đạt ngưỡng 10 triệu mà chưa có "
                   "người duyệt xác nhận. Khi bị từ chối, hãy dùng create_payment_ticket."),
    },
    "generate_business_report": {
        "description": "Kết xuất báo cáo chi phí theo tuần, tháng hoặc quý.",
        "when_to_use": "Khi người dùng yêu cầu báo cáo, thống kê hoặc tổng hợp chi phí theo kỳ.",
        "args": [("period_type", "str", "'week', 'month' hoặc 'quarter'"),
                 ("period_value", "str", "'2026-W30' | '2026-07' | '2026-Q3'")],
        "returns": "Tổng chi, số hóa đơn, phân bổ theo hạng mục và top nhà cung cấp.",
        "side_effect": "Không (chỉ đọc)",
        "example": 'generate_business_report["quarter", "2026-Q2"]',
        "errors": "Sai định dạng kỳ -> báo lỗi kèm ví dụ đúng.",
    },
}


# =============================================================================
# REGISTRY
# =============================================================================

AVAILABLE_TOOLS = {
    "list_invoice_files": list_invoice_files,
    "ocr_invoice": ocr_invoice,
    "search_policy": search_policy,
    "check_policy_compliance": check_policy_compliance,
    "check_duplicate_invoice": check_duplicate_invoice,
    "query_finance_db": query_finance_db,
    "get_my_invoice_status": get_my_invoice_status,
    "create_payment_ticket": create_payment_ticket,
    "approve_ticket": approve_ticket,
    "transfer_payment": transfer_payment,
    "generate_business_report": generate_business_report,
}


def get_tools_for_role(role: str) -> dict:
    """
    Lọc registry theo vai trò — LỚP PHÒNG THỦ SỐ 2.

    Nhân viên thường KHÔNG HỀ BIẾT tool transfer_payment tồn tại vì nó không
    xuất hiện trong System Prompt của họ.
    """
    allowed = roles.get_allowed_tools(role)
    return {name: fn for name, fn in AVAILABLE_TOOLS.items() if name in allowed}


def get_specs_for_role(role: str) -> dict:
    """Lọc TOOL_SPECS theo vai trò (dùng để sinh System Prompt)."""
    allowed = roles.get_allowed_tools(role)
    return {name: spec for name, spec in TOOL_SPECS.items() if name in allowed}


# =============================================================================
# SMOKE TEST — kiểm tra cả happy path lẫn error path
# =============================================================================

if __name__ == "__main__":
    print("=" * 78)
    print("🛠️  SMOKE TEST 11 TOOL — HAPPY PATH & ERROR PATH")
    print("=" * 78)

    # Reset DB để smoke test luôn bắt đầu từ trạng thái đã biết ➔ kết quả tái lập được.
    # (Chỉ reset trong smoke test; chạy app thật thì dùng init_db() giữ nguyên dữ liệu.)
    print("\n" + db.init_db(reset=True))
    # Tắt lỗi ngẫu nhiên của cổng thanh toán để nhãn test khớp kết quả.
    # (Các kịch bản lỗi ngân hàng đã được test riêng trong bank_api.py)
    os.environ["BANK_FAIL_RATE"] = "0"
    bank_api.reset_bank()

    def show(title: str, output: str, limit: int = 14):
        print(f"\n{'─' * 78}\n▶ {title}\n{'─' * 78}")
        lines = str(output).splitlines()
        for line in lines[:limit]:
            print("   " + line)
        if len(lines) > limit:
            print(f"   ... (còn {len(lines) - limit} dòng)")

    # ------------------------------------------------------------------ Kế toán
    set_session(roles.KE_TOAN, "KT-01")
    print(f"\n🔑 Phiên hiện tại: {get_session()['role']} / {get_session()['user_id']}")

    show("TOOL 1 — list_invoice_files", list_invoice_files())
    show("TOOL 2 — ocr_invoice (file không tồn tại)", ocr_invoice("khong_ton_tai.jpg"))
    show("TOOL 2b — ocr_invoice (thiếu tham số)", ocr_invoice(""))
    show("TOOL 3 — search_policy('ngưỡng phê duyệt')", search_policy("ngưỡng phê duyệt"), 10)
    show("TOOL 3b — search_policy (lạc đề)", search_policy("công thức nấu ăn"))

    show("TOOL 4 — check_policy_compliance (HỢP LỆ)",
         check_policy_compliance("Tiếp khách", "4750000", "0101245789", "2026-07-02"))
    show("TOOL 4b — check_policy_compliance (VƯỢT HẠN MỨC)",
         check_policy_compliance("Tiếp khách", "12500000", "0104433221", "2026-07-22"))
    show("TOOL 4c — check_policy_compliance (THIẾU MST)",
         check_policy_compliance("Thiết bị", "3200000", "", "2026-07-20"))
    show("TOOL 4d — check_policy_compliance (HẠNG MỤC SAI)",
         check_policy_compliance("Mua vàng", "1000000", "0101245789"))

    show("TOOL 5 — check_duplicate_invoice (ĐÃ THANH TOÁN)",
         check_duplicate_invoice("HD-2026-0013", "0101245789"))
    show("TOOL 5b — check_duplicate_invoice (CHƯA CÓ)",
         check_duplicate_invoice("HD-2026-9999", "0101245789"))

    show("TOOL 6 — query_finance_db('status','PENDING')",
         query_finance_db("status", "PENDING"))
    show("TOOL 6b — query_finance_db (BỘ LỌC SAI)", query_finance_db("gia_tien", "1000"))

    show("TOOL 7 — get_my_invoice_status('EMP-03')", get_my_invoice_status("EMP-03"))

    show("TOOL 11 — generate_business_report('quarter','2026-Q2')",
         generate_business_report("quarter", "2026-Q2"), 18)
    show("TOOL 11b — generate_business_report (SAI ĐỊNH DẠNG)",
         generate_business_report("month", "07/2026"))

    print("\n" + "=" * 78)
    print("🛡️  KIỂM TRA 4 CHỐT CHẶN CỦA transfer_payment (LỚP PHÒNG THỦ SỐ 4)")
    print("=" * 78)

    show("Chốt 1 — Hóa đơn ĐÃ THANH TOÁN",
         transfer_payment("HD-2026-0013", "Nhà hàng Sen Tây Hồ", "4750000",
                          "Tiếp khách", "2026-07-02", "0101245789"))
    show("Chốt 2 — THIẾU mã số thuế",
         transfer_payment("HD-2026-0018", "Xe khách Hoàng Long", "850000",
                          "Đi lại", "2026-07-24", ""))
    show("Chốt 3 — VƯỢT hạn mức hạng mục",
         transfer_payment("HD-2026-0017", "Nhà hàng Quán Ăn Ngon", "7200000",
                          "Tiếp khách", "2026-07-22", "0104433221"))
    # Dùng hạng mục 'Thuê ngoài' (hạn mức 20tr) với 15tr: NẰM TRONG hạn mức hạng mục
    # nhưng ĐẠT ngưỡng 10tr ➔ cô lập đúng chốt 4, không bị chốt 3 chặn trước.
    show("Chốt 4 — ĐẠT ngưỡng 10tr, CHƯA có người duyệt",
         transfer_payment("HD-2026-0021", "Công ty Bảo Vệ An Ninh", "15000000",
                          "Thuê ngoài", "2026-07-26", "0103334455"))

    show("✅ HỢP LỆ — dưới ngưỡng, đủ điều kiện",
         transfer_payment("HD-2026-0018", "Xe khách Hoàng Long", "850000",
                          "Đi lại", "2026-07-24", "0800112233"))
    show("🔁 Gọi LẠI hóa đơn vừa thanh toán (chống trùng)",
         transfer_payment("HD-2026-0018", "Xe khách Hoàng Long", "850000",
                          "Đi lại", "2026-07-24", "0800112233"))

    print("\n--- 🖐️ SAU KHI NGƯỜI DUYỆT XÁC NHẬN (grant_approval) ---")
    grant_approval(approval_key("HD-2026-0021", 15_000_000))
    show("Chốt 4 — ĐÃ có phê duyệt của con người",
         transfer_payment("HD-2026-0021", "Công ty Bảo Vệ An Ninh", "15000000",
                          "Thuê ngoài", "2026-07-26", "0103334455"))

    show("TOOL 8 — create_payment_ticket",
         create_payment_ticket("HD-2026-0017", "7200000", "Tài chính",
                               "Vượt hạn mức Tiếp khách 5 triệu, cần cấp trên phê duyệt"))
    show("TOOL 9 — approve_ticket (phiếu không tồn tại)", approve_ticket("999", "APPROVE"))
    show("TOOL 9b — approve_ticket (quyết định sai)", approve_ticket("1", "MAYBE"))

    # --------------------------------------------------------------- Nhân viên
    print("\n" + "=" * 78)
    print("🔓 KIỂM TRA LỚP PHÒNG THỦ SỐ 2 — LỌC TOOL THEO VAI")
    print("=" * 78)

    set_session(roles.NHAN_VIEN, "EMP-01")
    kt_tools = set(get_tools_for_role(roles.KE_TOAN))
    nv_tools = set(get_tools_for_role(roles.NHAN_VIEN))
    print(f"\n   Kế toán  thấy {len(kt_tools)} tool")
    print(f"   Nhân viên thấy {len(nv_tools)} tool")
    print(f"   Bị ẩn với nhân viên: {', '.join(sorted(kt_tools - nv_tools))}")

    show("Nhân viên xem hóa đơn NGƯỜI KHÁC (phải bị chặn)",
         get_my_invoice_status("EMP-03"))
    show("Nhân viên xem hóa đơn CHÍNH MÌNH (được phép)",
         get_my_invoice_status(""))

    print("\n--- 📋 NHẬT KÝ KIỂM TOÁN (5 bản ghi gần nhất) ---")
    for row in db.list_audit(limit=5):
        flag = "🚫" if row["blocked_reason"] else "✅"
        print(f"   {flag} [{row['ts']}] {row['role']}/{row['user_id']} → {row['tool']}")
        if row["blocked_reason"]:
            print(f"      Lý do chặn: {row['blocked_reason'][:100]}")

    print("\n" + "=" * 78)
    print("✅ tools.py — tất cả 11 tool hoạt động, không tool nào raise exception.")
    print("=" * 78)
