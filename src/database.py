"""
🗄️ TẦNG DỮ LIỆU — SQLite (data/expenses.db)

Bốn bảng:
  • invoices   — hóa đơn đã nhận dạng (OCR), trạng thái NEW/PENDING/PAID/REJECTED
  • payments   — giao dịch chuyển khoản đã thực hiện
  • tickets    — phiếu đề nghị duyệt chi liên phòng ban
  • audit_log  — 🔍 nhật ký MỌI hành động ghi, KỂ CẢ hành động bị chặn (Điều 7, QT-TC-03)

Thiết kế an toàn:
  • Ràng buộc UNIQUE(invoice_no, tax_code) chống thanh toán trùng ở TẦNG DATABASE
    (lớp phòng thủ sâu nhất — LLM không thể vượt qua bằng prompt injection).
  • KHÔNG có hàm nào nhận SQL thô từ bên ngoài. Agent chỉ gọi được các truy vấn
    đã định nghĩa sẵn với tham số bind — triệt tiêu hoàn toàn nguy cơ SQL injection.

Chạy độc lập:  python src/database.py
"""

import os
import sqlite3
import sys
from datetime import datetime

if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# =============================================================================
# ĐƯỜNG DẪN
# =============================================================================

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "expenses.db")

# Trạng thái hóa đơn
STATUS_NEW = "NEW"            # vừa nhận dạng, chưa xử lý
STATUS_PENDING = "PENDING"    # đang chờ duyệt (đã có ticket)
STATUS_PAID = "PAID"          # đã chuyển khoản thành công
STATUS_REJECTED = "REJECTED"  # bị từ chối

# Trạng thái phiếu đề nghị
TICKET_OPEN = "OPEN"
TICKET_APPROVED = "APPROVED"
TICKET_REJECTED = "REJECTED"


# =============================================================================
# KẾT NỐI
# =============================================================================

def get_connection() -> sqlite3.Connection:
    """Mở kết nối tới SQLite, bật foreign key và trả row dạng dict-like."""
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# =============================================================================
# KHỞI TẠO SCHEMA
# =============================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_no    TEXT    NOT NULL,
    vendor        TEXT    NOT NULL,
    tax_code      TEXT,
    category      TEXT    NOT NULL,
    amount        REAL    NOT NULL,
    invoice_date  TEXT    NOT NULL,
    source_image  TEXT,
    submitted_by  TEXT,
    department    TEXT,
    status        TEXT    NOT NULL DEFAULT 'NEW',
    created_at    TEXT    NOT NULL,
    UNIQUE (invoice_no, tax_code)
);

CREATE TABLE IF NOT EXISTS payments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id     INTEGER,
    invoice_no     TEXT    NOT NULL,
    vendor         TEXT    NOT NULL,
    category       TEXT,
    amount         REAL    NOT NULL,
    invoice_date   TEXT    NOT NULL,
    transaction_id TEXT,
    bank_status    TEXT    NOT NULL,
    paid_at        TEXT    NOT NULL,
    paid_by        TEXT,
    note           TEXT,
    FOREIGN KEY (invoice_id) REFERENCES invoices(id)
);

CREATE TABLE IF NOT EXISTS tickets (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_no        TEXT,
    vendor            TEXT,
    amount            REAL,
    requested_by      TEXT    NOT NULL,
    target_department TEXT    NOT NULL,
    reason            TEXT    NOT NULL,
    status            TEXT    NOT NULL DEFAULT 'OPEN',
    created_at        TEXT    NOT NULL,
    resolved_by       TEXT,
    resolved_at       TEXT,
    resolution_note   TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT    NOT NULL,
    role           TEXT,
    user_id        TEXT,
    tool           TEXT    NOT NULL,
    args           TEXT,
    result         TEXT,
    blocked_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_invoices_date   ON invoices(invoice_date);
CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(status);
CREATE INDEX IF NOT EXISTS idx_payments_date   ON payments(invoice_date);
CREATE INDEX IF NOT EXISTS idx_audit_ts        ON audit_log(ts);
"""


def init_db(reset: bool = False) -> str:
    """
    Tạo database và schema (idempotent — chạy bao nhiêu lần cũng an toàn).

    Args:
        reset (bool): True = xoá sạch dữ liệu cũ rồi seed lại từ đầu.

    Returns:
        str: Thông báo kết quả.
    """
    if reset and os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        seeded = _seed_if_empty(conn)
    finally:
        conn.close()

    action = "Đã tạo lại" if reset else "Sẵn sàng"
    return f"{action} database tại {DB_PATH}. {seeded}"


# =============================================================================
# DỮ LIỆU MẪU (MOCKDATA)
# =============================================================================
# Rải đều Q1–Q3/2026 để báo cáo theo tuần/tháng/quý đều có số liệu thật.
# (invoice_no, vendor, tax_code, category, amount, invoice_date, submitted_by, department, status)

_SEED_INVOICES = [
    # ---------- Quý 1/2026 ----------
    ("HD-2026-0001", "Nhà hàng Sen Tây Hồ",        "0101245789", "Tiếp khách", 4_500_000,  "2026-01-14", "EMP-02", "Sales",     STATUS_PAID),
    ("HD-2026-0002", "Vietnam Airlines",           "0100107518", "Đi lại",     2_850_000,  "2026-01-22", "EMP-01", "Marketing", STATUS_PAID),
    ("HD-2026-0003", "Khách sạn Mường Thanh",      "0102336699", "Khách sạn",  4_200_000,  "2026-02-05", "EMP-03", "Kỹ thuật",  STATUS_PAID),
    ("HD-2026-0004", "FPT Shop",                   "0101248141", "Thiết bị",   8_900_000,  "2026-02-18", "EMP-03", "Kỹ thuật",  STATUS_PAID),
    ("HD-2026-0005", "Cà phê Trung Nguyên",        "0304567891", "Ăn uống",      780_000,  "2026-03-03", "EMP-04", "Marketing", STATUS_PAID),
    ("HD-2026-0006", "Công ty TNHH Quảng Cáo VMG", "0106789123", "Thuê ngoài", 18_500_000, "2026-03-20", "EMP-01", "Marketing", STATUS_PAID),

    # ---------- Quý 2/2026 ----------
    ("HD-2026-0007", "Grab Việt Nam",              "0310966980", "Đi lại",     1_250_000,  "2026-04-08", "EMP-02", "Sales",     STATUS_PAID),
    ("HD-2026-0008", "Nhà hàng Ngon Garden",       "0105558822", "Tiếp khách", 4_950_000,  "2026-04-25", "EMP-02", "Sales",     STATUS_PAID),
    ("HD-2026-0009", "Khách sạn Melia Hà Nội",     "0100774433", "Khách sạn",  4_800_000,  "2026-05-11", "EMP-01", "Marketing", STATUS_PAID),
    ("HD-2026-0010", "Thế Giới Di Động",           "0303217354", "Thiết bị",   9_600_000,  "2026-05-29", "EMP-03", "Kỹ thuật",  STATUS_PAID),
    ("HD-2026-0011", "Cơm văn phòng Bếp Nhà",      "0109988776", "Ăn uống",      920_000,  "2026-06-10", "EMP-04", "Marketing", STATUS_PAID),
    ("HD-2026-0012", "Vietnam Airlines",           "0100107518", "Đi lại",     2_650_000,  "2026-06-24", "EMP-02", "Sales",     STATUS_PAID),

    # ---------- Quý 3/2026 ----------
    ("HD-2026-0013", "Nhà hàng Sen Tây Hồ",        "0101245789", "Tiếp khách", 4_750_000,  "2026-07-02", "EMP-02", "Sales",     STATUS_PAID),
    ("HD-2026-0014", "Highlands Coffee",           "0302145879", "Ăn uống",      650_000,  "2026-07-09", "EMP-01", "Marketing", STATUS_PAID),
    ("HD-2026-0015", "Khách sạn Pullman",          "0101667788", "Khách sạn",  4_950_000,  "2026-07-16", "EMP-04", "Marketing", STATUS_PAID),

    # --- Chưa thanh toán: để Agent có việc để làm khi demo ---
    ("HD-2026-0016", "Công ty CP Thiết Bị Á Châu", "0107654321", "Thiết bị",  12_800_000,  "2026-07-20", "EMP-03", "Kỹ thuật",  STATUS_PENDING),
    ("HD-2026-0017", "Nhà hàng Quán Ăn Ngon",      "0104433221", "Tiếp khách", 7_200_000,  "2026-07-22", "EMP-02", "Sales",     STATUS_NEW),
    ("HD-2026-0018", "Xe khách Hoàng Long",        "0800112233", "Đi lại",       850_000,  "2026-07-24", "EMP-04", "Marketing", STATUS_NEW),
    ("HD-2026-0019", "Dịch vụ vệ sinh Sạch Xanh",  "0109876543", "Thuê ngoài", 22_000_000, "2026-07-25", "EMP-01", "Marketing", STATUS_PENDING),
    ("HD-2026-0020", "Cửa hàng VP Phẩm Hồng Hà",   "0101010101", "Thiết bị",     450_000,  "2026-07-27", "EMP-03", "Kỹ thuật",  STATUS_NEW),

    # ⚠️ Hóa đơn ĐẶC BIỆT phục vụ demo Human-in-the-loop:
    # 15tr NẰM TRONG hạn mức Thuê ngoài (20tr) nhưng ĐẠT ngưỡng duyệt tay (10tr).
    # Nhờ vậy nó vượt qua chốt 3 và dừng đúng ở chốt 4 — cô lập được lớp phòng thủ số 4.
    ("HD-2026-0021", "Công ty Bảo Vệ An Ninh Sài Gòn", "0103334455", "Thuê ngoài", 15_000_000, "2026-07-26", "EMP-01", "Marketing", STATUS_NEW),
]

# (invoice_no, requested_by, target_department, reason, status)
_SEED_TICKETS = [
    ("HD-2026-0016", "EMP-03", "Tài chính",
     "Hóa đơn thiết bị 12.800.000đ vượt ngưỡng duyệt tự động 10.000.000đ, cần Trưởng phòng Tài chính phê duyệt.",
     TICKET_OPEN),
    ("HD-2026-0019", "EMP-01", "Ban Giám đốc",
     "Hợp đồng thuê ngoài 22.000.000đ vượt hạn mức Thuê ngoài 20.000.000đ, cần Giám đốc phê duyệt.",
     TICKET_OPEN),
    ("HD-2026-0006", "EMP-01", "Tài chính",
     "Đề nghị bổ sung ngân sách Marketing Quý 1 do chi phí quảng cáo vượt kế hoạch.",
     TICKET_APPROVED),
]


def _seed_if_empty(conn: sqlite3.Connection) -> str:
    """Chỉ seed khi bảng invoices đang rỗng — tránh nhân bản dữ liệu khi chạy lại."""
    count = conn.execute("SELECT COUNT(*) AS c FROM invoices").fetchone()["c"]
    if count > 0:
        return f"Giữ nguyên {count} hóa đơn sẵn có (không seed lại)."

    now = datetime.now().isoformat(timespec="seconds")

    for inv in _SEED_INVOICES:
        conn.execute(
            """INSERT INTO invoices
               (invoice_no, vendor, tax_code, category, amount, invoice_date,
                source_image, submitted_by, department, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)""",
            (inv[0], inv[1], inv[2], inv[3], inv[4], inv[5], inv[6], inv[7], inv[8], now),
        )

    # Sinh payment tương ứng cho mọi hóa đơn đã PAID
    paid = conn.execute(
        "SELECT id, invoice_no, vendor, category, amount, invoice_date FROM invoices WHERE status = ?",
        (STATUS_PAID,),
    ).fetchall()

    for idx, row in enumerate(paid, start=1):
        txn = f"TXN{row['invoice_date'].replace('-', '')}{idx:04d}"
        conn.execute(
            """INSERT INTO payments
               (invoice_id, invoice_no, vendor, category, amount, invoice_date,
                transaction_id, bank_status, paid_at, paid_by, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'SUCCESS', ?, 'KT-01', 'Dữ liệu lịch sử')""",
            (row["id"], row["invoice_no"], row["vendor"], row["category"],
             row["amount"], row["invoice_date"], txn, row["invoice_date"] + "T10:00:00"),
        )

    for tk in _SEED_TICKETS:
        inv = conn.execute(
            "SELECT vendor, amount FROM invoices WHERE invoice_no = ?", (tk[0],)
        ).fetchone()
        conn.execute(
            """INSERT INTO tickets
               (invoice_no, vendor, amount, requested_by, target_department,
                reason, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (tk[0], inv["vendor"] if inv else None, inv["amount"] if inv else None,
             tk[1], tk[2], tk[3], tk[4], now),
        )

    conn.commit()
    return f"Đã seed {len(_SEED_INVOICES)} hóa đơn, {len(paid)} giao dịch, {len(_SEED_TICKETS)} phiếu đề nghị."


# =============================================================================
# TRUY VẤN HÓA ĐƠN
# =============================================================================

def get_invoice_by_no(invoice_no: str) -> dict:
    """Tra hóa đơn theo số hóa đơn. Không có -> trả về {}."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM invoices WHERE UPPER(invoice_no) = UPPER(?)", (invoice_no.strip(),)
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def find_duplicate_invoice(invoice_no: str = "", tax_code: str = "",
                           vendor: str = "", amount: float = 0,
                           invoice_date: str = "") -> list:
    """
    Tìm hóa đơn trùng theo 2 tiêu chí của Điều 5, QD-TC-02:
      1. Trùng CHÍNH XÁC (invoice_no + tax_code)  -> chắc chắn trùng
      2. Trùng NGHI VẤN  (vendor + amount + date) -> cần người xem lại

    Returns:
        list[dict]: Danh sách hóa đơn nghi trùng, kèm khoá 'match_type'.
    """
    conn = get_connection()
    results = []
    try:
        if invoice_no and tax_code:
            rows = conn.execute(
                """SELECT * FROM invoices
                   WHERE UPPER(invoice_no) = UPPER(?) AND tax_code = ?""",
                (invoice_no.strip(), tax_code.strip()),
            ).fetchall()
            for r in rows:
                d = dict(r)
                d["match_type"] = "EXACT"
                results.append(d)

        if vendor and amount:
            # Loại trừ CHÍNH hóa đơn đang xét — trùng với bản thân mình không phải nghi vấn
            rows = conn.execute(
                """SELECT * FROM invoices
                   WHERE vendor = ? AND ABS(amount - ?) < 1
                     AND (? = '' OR invoice_date = ?)
                     AND (? = '' OR UPPER(invoice_no) != UPPER(?))""",
                (vendor.strip(), float(amount), invoice_date, invoice_date,
                 invoice_no.strip(), invoice_no.strip()),
            ).fetchall()
            seen = {r["id"] for r in results} if results else set()
            for r in rows:
                if r["id"] not in seen:
                    d = dict(r)
                    d["match_type"] = "SIMILAR"
                    results.append(d)
        return results
    finally:
        conn.close()


def insert_invoice(invoice_no: str, vendor: str, tax_code: str, category: str,
                   amount: float, invoice_date: str, source_image: str = "",
                   submitted_by: str = "", department: str = "",
                   status: str = STATUS_NEW) -> dict:
    """
    Thêm hóa đơn mới. Vi phạm UNIQUE(invoice_no, tax_code) -> trả về lỗi thay vì raise.

    Returns:
        dict: {"ok": bool, "id": int|None, "message": str}
    """
    conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO invoices
               (invoice_no, vendor, tax_code, category, amount, invoice_date,
                source_image, submitted_by, department, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (invoice_no, vendor, tax_code, category, float(amount), invoice_date,
             source_image, submitted_by, department, status,
             datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
        return {"ok": True, "id": cur.lastrowid, "message": "Đã lưu hóa đơn."}
    except sqlite3.IntegrityError:
        return {
            "ok": False,
            "id": None,
            "message": (f"Hóa đơn '{invoice_no}' của nhà cung cấp có MST '{tax_code}' "
                        f"ĐÃ TỒN TẠI trong hệ thống (ràng buộc chống trùng ở tầng database)."),
        }
    finally:
        conn.close()


def update_invoice_status(invoice_no: str, status: str) -> bool:
    """Cập nhật trạng thái hóa đơn. Trả về True nếu có dòng bị ảnh hưởng."""
    conn = get_connection()
    try:
        cur = conn.execute(
            "UPDATE invoices SET status = ? WHERE UPPER(invoice_no) = UPPER(?)",
            (status, invoice_no.strip()),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_invoices_by_user(user_id: str, limit: int = 50) -> list:
    """Danh sách hóa đơn do MỘT người nộp (dùng cho vai Nhân viên — Điều 4, QD-TC-04)."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT * FROM invoices WHERE submitted_by = ?
               ORDER BY invoice_date DESC LIMIT ?""",
            (user_id.strip(), limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# =============================================================================
# TRUY VẤN SỔ CÁI (chỉ dành cho Kế toán)
# =============================================================================

# Các bộ lọc hợp lệ — Agent KHÔNG thể truyền SQL thô vào đây.
VALID_FILTERS = {
    "vendor": "Lọc theo tên nhà cung cấp (khớp gần đúng)",
    "category": "Lọc theo hạng mục chi phí",
    "status": "Lọc theo trạng thái hóa đơn (NEW/PENDING/PAID/REJECTED)",
    "employee": "Lọc theo mã nhân viên nộp hóa đơn",
    "department": "Lọc theo phòng ban",
    "date_range": "Lọc theo khoảng ngày, giá trị dạng 'YYYY-MM-DD..YYYY-MM-DD'",
    "all": "Lấy toàn bộ (giới hạn 50 dòng gần nhất)",
}


def query_invoices(filter_type: str, value: str = "", limit: int = 50) -> list:
    """
    Truy vấn sổ cái hóa đơn theo bộ lọc ĐÃ ĐỊNH NGHĨA SẴN.

    ⚠️ Cố tình KHÔNG nhận SQL thô — mọi giá trị đều bind tham số,
       loại bỏ hoàn toàn nguy cơ SQL injection qua LLM.
    """
    filter_type = (filter_type or "all").strip().lower()
    if filter_type not in VALID_FILTERS:
        return []

    conn = get_connection()
    try:
        if filter_type == "all":
            sql = "SELECT * FROM invoices ORDER BY invoice_date DESC LIMIT ?"
            params = (limit,)
        elif filter_type == "vendor":
            sql = "SELECT * FROM invoices WHERE vendor LIKE ? ORDER BY invoice_date DESC LIMIT ?"
            params = (f"%{value.strip()}%", limit)
        elif filter_type == "category":
            sql = "SELECT * FROM invoices WHERE category = ? ORDER BY invoice_date DESC LIMIT ?"
            params = (value.strip(), limit)
        elif filter_type == "status":
            sql = "SELECT * FROM invoices WHERE status = ? ORDER BY invoice_date DESC LIMIT ?"
            params = (value.strip().upper(), limit)
        elif filter_type == "employee":
            sql = "SELECT * FROM invoices WHERE submitted_by = ? ORDER BY invoice_date DESC LIMIT ?"
            params = (value.strip().upper(), limit)
        elif filter_type == "department":
            sql = "SELECT * FROM invoices WHERE department = ? ORDER BY invoice_date DESC LIMIT ?"
            params = (value.strip(), limit)
        else:  # date_range
            parts = value.split("..")
            if len(parts) != 2:
                return []
            sql = ("SELECT * FROM invoices WHERE invoice_date BETWEEN ? AND ? "
                   "ORDER BY invoice_date DESC LIMIT ?")
            params = (parts[0].strip(), parts[1].strip(), limit)

        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


# =============================================================================
# GIAO DỊCH THANH TOÁN
# =============================================================================

def insert_payment(invoice_no: str, vendor: str, category: str, amount: float,
                   invoice_date: str, transaction_id: str, bank_status: str,
                   paid_by: str = "", note: str = "", invoice_id=None) -> int:
    """Ghi nhận một giao dịch chuyển khoản. Trả về id bản ghi vừa tạo."""
    conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO payments
               (invoice_id, invoice_no, vendor, category, amount, invoice_date,
                transaction_id, bank_status, paid_at, paid_by, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (invoice_id, invoice_no, vendor, category, float(amount), invoice_date,
             transaction_id, bank_status,
             datetime.now().isoformat(timespec="seconds"), paid_by, note),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def is_already_paid(invoice_no: str) -> dict:
    """Hóa đơn này đã có giao dịch SUCCESS chưa? Có -> trả về bản ghi payment."""
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT * FROM payments
               WHERE UPPER(invoice_no) = UPPER(?) AND bank_status = 'SUCCESS'
               ORDER BY paid_at DESC LIMIT 1""",
            (invoice_no.strip(),),
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


# =============================================================================
# PHIẾU ĐỀ NGHỊ (TICKET)
# =============================================================================

def create_ticket(invoice_no: str, vendor: str, amount: float, requested_by: str,
                  target_department: str, reason: str) -> int:
    """Lập phiếu đề nghị duyệt chi liên phòng ban (Điều 4, QT-TC-03)."""
    conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO tickets
               (invoice_no, vendor, amount, requested_by, target_department,
                reason, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'OPEN', ?)""",
            (invoice_no, vendor, amount, requested_by, target_department, reason,
             datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_tickets(status: str = "", requested_by: str = "") -> list:
    """Liệt kê phiếu đề nghị, lọc theo trạng thái và/hoặc người lập."""
    conn = get_connection()
    try:
        sql = "SELECT * FROM tickets WHERE 1 = 1"
        params = []
        if status:
            sql += " AND status = ?"
            params.append(status.strip().upper())
        if requested_by:
            sql += " AND requested_by = ?"
            params.append(requested_by.strip().upper())
        sql += " ORDER BY created_at DESC LIMIT 50"
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def get_ticket(ticket_id) -> dict:
    """Tra phiếu đề nghị theo id."""
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM tickets WHERE id = ?", (int(ticket_id),)).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def resolve_ticket(ticket_id, status: str, resolved_by: str, note: str = "") -> bool:
    """Phê duyệt hoặc từ chối phiếu đề nghị. Chỉ tác động lên phiếu đang OPEN."""
    conn = get_connection()
    try:
        cur = conn.execute(
            """UPDATE tickets
               SET status = ?, resolved_by = ?, resolved_at = ?, resolution_note = ?
               WHERE id = ? AND status = 'OPEN'""",
            (status.strip().upper(), resolved_by,
             datetime.now().isoformat(timespec="seconds"), note, int(ticket_id)),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# =============================================================================
# NHẬT KÝ KIỂM TOÁN (Điều 7, QT-TC-03)
# =============================================================================

def write_audit(tool: str, role: str = "", user_id: str = "", args: str = "",
                result: str = "", blocked_reason: str = "") -> int:
    """
    Ghi nhật ký MỘT hành động — kể cả hành động BỊ CHẶN.

    `blocked_reason` khác rỗng nghĩa là hành động đã bị hệ thống từ chối.
    Đây là bằng chứng quan trọng nhất cho tiêu chí Observability.
    """
    conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO audit_log (ts, role, user_id, tool, args, result, blocked_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (datetime.now().isoformat(timespec="seconds"), role, user_id, tool,
             str(args)[:500], str(result)[:500], blocked_reason),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_audit(limit: int = 50, only_blocked: bool = False) -> list:
    """Đọc nhật ký kiểm toán, mới nhất trước."""
    conn = get_connection()
    try:
        sql = "SELECT * FROM audit_log"
        if only_blocked:
            sql += " WHERE blocked_reason IS NOT NULL AND blocked_reason != ''"
        sql += " ORDER BY id DESC LIMIT ?"
        return [dict(r) for r in conn.execute(sql, (limit,)).fetchall()]
    finally:
        conn.close()


# =============================================================================
# BÁO CÁO TÀI CHÍNH THEO TUẦN / THÁNG / QUÝ
# =============================================================================

PERIOD_TYPES = ("week", "month", "quarter")


def _period_to_range(period_type: str, period_value: str):
    """
    Chuyển kỳ báo cáo thành mệnh đề SQL lọc theo invoice_date.

    Định dạng period_value:
      • week    : '2026-W30'  (tuần ISO theo SQLite %W, tuần bắt đầu từ Thứ Hai)
      • month   : '2026-07'
      • quarter : '2026-Q3'

    Returns:
        (where_clause, params, mô_tả_kỳ) hoặc (None, None, thông_báo_lỗi)
    """
    period_type = (period_type or "").strip().lower()
    period_value = (period_value or "").strip().upper()

    if period_type == "month":
        if len(period_value) != 7 or period_value[4] != "-":
            return None, None, "Kỳ tháng phải có dạng 'YYYY-MM', ví dụ '2026-07'."
        return ("strftime('%Y-%m', invoice_date) = ?", [period_value.lower()],
                f"Tháng {period_value[5:7]}/{period_value[:4]}")

    if period_type == "week":
        if "W" not in period_value:
            return None, None, "Kỳ tuần phải có dạng 'YYYY-Wnn', ví dụ '2026-W30'."
        year, week = period_value.split("W", 1)
        year = year.rstrip("-")
        if not (year.isdigit() and week.isdigit()):
            return None, None, "Kỳ tuần phải có dạng 'YYYY-Wnn', ví dụ '2026-W30'."
        return ("strftime('%Y', invoice_date) = ? AND strftime('%W', invoice_date) = ?",
                [year, f"{int(week):02d}"],
                f"Tuần {int(week)} năm {year}")

    if period_type == "quarter":
        if "Q" not in period_value:
            return None, None, "Kỳ quý phải có dạng 'YYYY-Qn', ví dụ '2026-Q3'."
        year, quarter = period_value.split("Q", 1)
        year = year.rstrip("-")
        if not (year.isdigit() and quarter.isdigit() and 1 <= int(quarter) <= 4):
            return None, None, "Kỳ quý phải có dạng 'YYYY-Qn' với n từ 1 đến 4, ví dụ '2026-Q3'."
        return ("strftime('%Y', invoice_date) = ? "
                "AND (CAST(strftime('%m', invoice_date) AS INTEGER) + 2) / 3 = ?",
                [year, int(quarter)],
                f"Quý {quarter}/{year}")

    return None, None, (f"Loại kỳ báo cáo '{period_type}' không hợp lệ. "
                        f"Chỉ chấp nhận: {', '.join(PERIOD_TYPES)}.")


def financial_report(period_type: str, period_value: str) -> dict:
    """
    Tổng hợp báo cáo tài chính từ bảng `payments` (giao dịch THÀNH CÔNG).

    Returns:
        dict: {ok, period_label, total, count, by_category, top_vendors} hoặc {ok: False, error}
    """
    where, params, label = _period_to_range(period_type, period_value)
    if where is None:
        return {"ok": False, "error": label}

    conn = get_connection()
    try:
        base = f"FROM payments WHERE bank_status = 'SUCCESS' AND {where}"

        summary = conn.execute(
            f"SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS cnt {base}", params
        ).fetchone()

        by_cat = conn.execute(
            f"""SELECT category, SUM(amount) AS total, COUNT(*) AS cnt {base}
                GROUP BY category ORDER BY total DESC""", params
        ).fetchall()

        top_vendors = conn.execute(
            f"""SELECT vendor, SUM(amount) AS total, COUNT(*) AS cnt {base}
                GROUP BY vendor ORDER BY total DESC LIMIT 3""", params
        ).fetchall()

        return {
            "ok": True,
            "period_label": label,
            "period_type": period_type.lower(),
            "period_value": period_value,
            "total": float(summary["total"]),
            "count": int(summary["cnt"]),
            "by_category": [dict(r) for r in by_cat],
            "top_vendors": [dict(r) for r in top_vendors],
        }
    finally:
        conn.close()


def format_money(amount) -> str:
    """Định dạng tiền VNĐ: 1234567 -> '1.234.567đ'"""
    try:
        return f"{int(round(float(amount))):,}".replace(",", ".") + "đ"
    except (TypeError, ValueError):
        return str(amount)


# =============================================================================
# SMOKE TEST
# =============================================================================

if __name__ == "__main__":
    print("=" * 70)
    print("🗄️  KHỞI TẠO & KIỂM TRA DATABASE")
    print("=" * 70)

    print("\n" + init_db())

    conn = get_connection()
    for table in ("invoices", "payments", "tickets", "audit_log"):
        n = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
        print(f"  • {table:<12}: {n} dòng")
    conn.close()

    print("\n--- 📊 BÁO CÁO THỬ ---")
    for ptype, pvalue in [("quarter", "2026-Q2"), ("month", "2026-07"), ("week", "2026-W28")]:
        rep = financial_report(ptype, pvalue)
        if rep["ok"]:
            print(f"\n▶ {rep['period_label']}: {format_money(rep['total'])} / {rep['count']} hóa đơn")
            for c in rep["by_category"]:
                print(f"    - {c['category']:<12}: {format_money(c['total'])} ({c['cnt']} HĐ)")
        else:
            print(f"\n▶ LỖI: {rep['error']}")

    print("\n--- ❌ TEST KỲ BÁO CÁO SAI ĐỊNH DẠNG ---")
    print("  ", financial_report("month", "07/2026")["error"])
    print("  ", financial_report("nam", "2026")["error"])

    print("\n--- 🔁 TEST CHỐNG TRÙNG Ở TẦNG DATABASE ---")
    dup = insert_invoice("HD-2026-0001", "Nhà hàng Sen Tây Hồ", "0101245789",
                         "Tiếp khách", 4_500_000, "2026-01-14")
    print("  ", dup["message"])

    print("\n--- 🔍 TEST TÌM HÓA ĐƠN TRÙNG ---")
    for d in find_duplicate_invoice(invoice_no="HD-2026-0013", tax_code="0101245789"):
        print(f"   [{d['match_type']}] {d['invoice_no']} | {d['vendor']} | "
              f"{format_money(d['amount'])} | {d['status']}")

    print("\n✅ database.py hoạt động bình thường.")
