"""
💰 API CHUYỂN KHOẢN GIẢ LẬP

Mô phỏng cổng thanh toán ngân hàng để demo luồng thanh toán tự động mà KHÔNG
đụng tới tiền thật. Cố tình sinh ra lỗi giao dịch có thật trong đời sống
(số dư không đủ, sai số tài khoản, timeout) để chứng minh Agent biết đọc mã lỗi
và đổi hướng theo Điều 5 QT-TC-03, thay vì retry mù quáng.

Tính TÁI LẬP: dùng seed ngẫu nhiên cố định (BANK_RANDOM_SEED) ➔ khi demo trước
lớp, kịch bản lỗi lặp lại y hệt, không bị "lúc chạy được lúc không".

Chạy độc lập:  python src/bank_api.py
"""

import os
import random
import re
import sys
import time
from datetime import datetime

from dotenv import load_dotenv

if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

load_dotenv()

# =============================================================================
# MÃ TRẠNG THÁI GIAO DỊCH
# =============================================================================

STATUS_SUCCESS = "SUCCESS"
STATUS_INSUFFICIENT = "INSUFFICIENT_FUNDS"
STATUS_INVALID_ACCOUNT = "INVALID_ACCOUNT"
STATUS_TIMEOUT = "TIMEOUT"
STATUS_INVALID_AMOUNT = "INVALID_AMOUNT"

STATUS_MESSAGES = {
    STATUS_SUCCESS: "Giao dịch thành công.",
    STATUS_INSUFFICIENT: "Số dư tài khoản công ty không đủ để thực hiện giao dịch.",
    STATUS_INVALID_ACCOUNT: "Số tài khoản thụ hưởng không hợp lệ.",
    STATUS_TIMEOUT: "Cổng thanh toán không phản hồi (hết thời gian chờ).",
    STATUS_INVALID_AMOUNT: "Số tiền chuyển khoản không hợp lệ.",
}

# Số tài khoản hợp lệ: 6-20 chữ số
_RE_ACCOUNT = re.compile(r"^\d{6,20}$")

# Trạng thái số dư trong phiên (reset khi khởi động lại app)
_session_balance = None
_rng = None


# =============================================================================
# CẤU HÌNH
# =============================================================================

def get_config() -> dict:
    """Đọc cấu hình ngân hàng giả lập từ .env."""
    def _float(name, default):
        try:
            return float(os.getenv(name) or default)
        except ValueError:
            return default

    def _int(name, default):
        try:
            return int(float(os.getenv(name) or default))
        except ValueError:
            return default

    return {
        "fail_rate": max(0.0, min(1.0, _float("BANK_FAIL_RATE", 0.1))),
        "initial_balance": _float("BANK_COMPANY_BALANCE", 500_000_000),
        "seed": _int("BANK_RANDOM_SEED", 42),
    }


def _get_rng() -> random.Random:
    """Bộ sinh ngẫu nhiên riêng, seed cố định ➔ kịch bản lỗi tái lập được."""
    global _rng
    if _rng is None:
        _rng = random.Random(get_config()["seed"])
    return _rng


def get_balance() -> float:
    """Số dư hiện tại của tài khoản công ty (giả lập)."""
    global _session_balance
    if _session_balance is None:
        _session_balance = get_config()["initial_balance"]
    return _session_balance


def reset_bank(balance: float = None):
    """Đặt lại số dư và bộ sinh ngẫu nhiên — dùng khi bắt đầu một phiên demo mới."""
    global _session_balance, _rng
    _session_balance = balance if balance is not None else get_config()["initial_balance"]
    _rng = random.Random(get_config()["seed"])


def format_money(amount) -> str:
    """1234567 -> '1.234.567đ'"""
    try:
        return f"{int(round(float(amount))):,}".replace(",", ".") + "đ"
    except (TypeError, ValueError):
        return str(amount)


# =============================================================================
# CHUYỂN KHOẢN
# =============================================================================

def transfer(account_no: str, amount, content: str = "", vendor: str = "",
             simulate_latency: bool = True) -> dict:
    """
    Thực hiện lệnh chuyển khoản giả lập.

    Args:
        account_no (str): Số tài khoản thụ hưởng (6-20 chữ số)
        amount (float|str): Số tiền chuyển (VNĐ)
        content (str): Nội dung chuyển khoản
        vendor (str): Tên đơn vị thụ hưởng (để ghi log)
        simulate_latency (bool): Có giả lập độ trễ mạng không (tắt khi chạy test)

    Returns:
        dict: {ok, status, transaction_id, message, amount, balance_after, timestamp}

    ⚠️ KHÔNG BAO GIỜ raise — mọi lỗi đều trả về dưới dạng mã trạng thái.
    """
    global _session_balance

    timestamp = datetime.now().isoformat(timespec="seconds")
    rng = _get_rng()
    cfg = get_config()

    def _fail(status: str, extra: str = "") -> dict:
        return {
            "ok": False,
            "status": status,
            "transaction_id": None,
            "message": STATUS_MESSAGES[status] + (f" {extra}" if extra else ""),
            "amount": amount,
            "balance_after": get_balance(),
            "timestamp": timestamp,
        }

    # --- 1. Kiểm tra số tiền ---
    try:
        amount = float(str(amount).replace(",", "").replace("đ", "").strip())
    except (TypeError, ValueError):
        return _fail(STATUS_INVALID_AMOUNT, f"Không đọc được giá trị '{amount}'.")

    if amount <= 0:
        return _fail(STATUS_INVALID_AMOUNT, "Số tiền phải lớn hơn 0.")

    # --- 2. Kiểm tra số tài khoản ---
    account_no = str(account_no or "").strip().replace(" ", "").replace("-", "")
    if not _RE_ACCOUNT.match(account_no):
        return _fail(STATUS_INVALID_ACCOUNT,
                     f"'{account_no}' phải gồm 6-20 chữ số, không chứa chữ cái.")

    # --- 3. Giả lập độ trễ mạng ---
    if simulate_latency:
        time.sleep(rng.uniform(0.3, 1.2))

    # --- 4. Giả lập timeout ngẫu nhiên ---
    if rng.random() < cfg["fail_rate"]:
        return _fail(STATUS_TIMEOUT,
                     "Vui lòng KIỂM TRA LẠI trạng thái giao dịch trước khi thử lại — "
                     "tuyệt đối không gửi lại lệnh ngay vì có thể chuyển tiền hai lần.")

    # --- 5. Kiểm tra số dư ---
    balance = get_balance()
    if amount > balance:
        return _fail(STATUS_INSUFFICIENT,
                     f"Cần {format_money(amount)} nhưng chỉ còn {format_money(balance)}. "
                     f"Theo Điều 5 QT-TC-03, hãy lập phiếu đề nghị gửi Phòng Tài chính "
                     f"thay vì thử lại lệnh chuyển khoản.")

    # --- 6. Thành công ---
    _session_balance = balance - amount
    txn_id = f"TXN{datetime.now().strftime('%Y%m%d%H%M%S')}{rng.randint(100, 999)}"

    return {
        "ok": True,
        "status": STATUS_SUCCESS,
        "transaction_id": txn_id,
        "message": (f"Đã chuyển {format_money(amount)} tới tài khoản {account_no}"
                    + (f" ({vendor})" if vendor else "")
                    + f". Mã giao dịch: {txn_id}."),
        "amount": amount,
        "account_no": account_no,
        "vendor": vendor,
        "content": content,
        "balance_after": _session_balance,
        "timestamp": timestamp,
    }


def lookup_account(vendor: str) -> str:
    """
    Tra số tài khoản của nhà cung cấp trong "danh bạ" giả lập.

    Nhà cung cấp lạ -> sinh số tài khoản giả ổn định theo tên (hash),
    để cùng một nhà cung cấp luôn ra cùng một số tài khoản khi demo.
    """
    directory = {
        "Nhà hàng Sen Tây Hồ": "19001234567",
        "Vietnam Airlines": "10203040506",
        "FPT Shop": "11220033445",
        "Thế Giới Di Động": "99887766554",
        "Grab Việt Nam": "12345098765",
        "Khách sạn Melia Hà Nội": "55667788990",
    }
    vendor = (vendor or "").strip()
    if vendor in directory:
        return directory[vendor]

    if not vendor:
        return ""
    stable = abs(hash(vendor.lower())) % (10 ** 11)
    return f"{stable:011d}"


# =============================================================================
# SMOKE TEST
# =============================================================================

if __name__ == "__main__":
    print("=" * 70)
    print("💰 KIỂM TRA API CHUYỂN KHOẢN GIẢ LẬP")
    print("=" * 70)

    cfg = get_config()
    reset_bank()
    print(f"\n⚙️  Cấu hình:")
    print(f"   Số dư ban đầu : {format_money(cfg['initial_balance'])}")
    print(f"   Tỉ lệ lỗi     : {cfg['fail_rate'] * 100:.0f}%")
    print(f"   Seed ngẫu nhiên: {cfg['seed']} (cố định ➔ kịch bản tái lập được)")

    cases = [
        ("Giao dịch hợp lệ",        "19001234567", 4_500_000),
        ("Số tài khoản sai",        "ABC-XYZ",     1_000_000),
        ("Số tiền âm",              "19001234567", -500),
        ("Số tiền không đọc được",  "19001234567", "abc"),
        ("Vượt số dư công ty",      "19001234567", 900_000_000),
    ]

    print("\n--- 🧪 CÁC KỊCH BẢN GIAO DỊCH ---")
    for label, acc, amt in cases:
        res = transfer(acc, amt, content="Test", simulate_latency=False)
        icon = "✅" if res["ok"] else "❌"
        print(f"\n{icon} {label}")
        print(f"   Trạng thái: {res['status']}")
        print(f"   Thông báo : {res['message']}")

    print("\n--- 🔁 CHẠY 12 GIAO DỊCH NHỎ ĐỂ THẤY TIMEOUT NGẪU NHIÊN ---")
    reset_bank()
    stats = {}
    for i in range(1, 13):
        res = transfer("19001234567", 1_000_000, content=f"GD {i}", simulate_latency=False)
        stats[res["status"]] = stats.get(res["status"], 0) + 1
        icon = "✅" if res["ok"] else "⚠️"
        print(f"   {icon} GD {i:>2}: {res['status']:<20} | Số dư còn {format_money(res['balance_after'])}")

    print(f"\n   Thống kê: {stats}")

    print("\n--- 📒 TRA SỐ TÀI KHOẢN NHÀ CUNG CẤP ---")
    for v in ["Nhà hàng Sen Tây Hồ", "FPT Shop", "Công ty Lạ Hoắc ABC"]:
        print(f"   {v:<28} -> {lookup_account(v)}")

    print("\n✅ bank_api.py hoạt động bình thường.")
