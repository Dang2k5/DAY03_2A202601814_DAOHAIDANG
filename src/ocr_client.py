"""
📷 OCR CLIENT — NHẬN DẠNG HÓA ĐƠN

Hỗ trợ HAI chế độ, chọn bằng biến OCR_MODE trong .env:

  • OCR_MODE=llm  (MẶC ĐỊNH) — dùng LLM đa phương thức (GPT-4o Vision).
    Gửi thẳng ảnh cho model và yêu cầu trả về JSON 6 trường nghiệp vụ.
    Ưu điểm: không cần dựng service riêng, đọc được hóa đơn viết tay/ảnh chụp lệch,
    hiểu được ngữ cảnh tiếng Việt (phân biệt "Tổng cộng" với "Cộng tiền hàng").

  • OCR_MODE=http — gọi service OCR chạy ở máy khác trong mạng LAN (cổng 8080).
    Giữ lại làm phương án dự phòng khi có sẵn hạ tầng OCR nội bộ.

NGUYÊN TẮC BẤT BIẾN: hàm này KHÔNG BAO GIỜ raise. Mọi sự cố (thiếu API key, ảnh hỏng,
model trả sai định dạng, service chết) đều trở thành chuỗi "LỖI OCR: ..." để Agent đọc
và tự quyết định đổi hướng — TUYỆT ĐỐI không để Agent bịa số liệu.

Chạy độc lập:  python src/ocr_client.py
"""

import base64
import json
import os
import re
import sys

import requests
from dotenv import load_dotenv

if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

load_dotenv()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INVOICE_DIR = os.path.join(BASE_DIR, "data", "invoices")

# --- Chế độ HTTP (dự phòng) ---
CANDIDATE_ENDPOINTS = ["/ocr", "/predict", "/v1/ocr", "/api/ocr", "/upload", "/"]
CANDIDATE_FIELDS = ["file", "image", "img", "upload"]

SUPPORTED_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".pdf", ".tif", ".tiff")

# LLM Vision chỉ nhận được ảnh raster, không nhận PDF trực tiếp
VISION_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")

MIME_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
}


# =============================================================================
# CẤU HÌNH
# =============================================================================

def get_config() -> dict:
    """Đọc cấu hình OCR từ biến môi trường (.env)."""
    mode = (os.getenv("OCR_MODE") or "llm").strip().lower()
    if mode not in ("llm", "http"):
        mode = "llm"

    base_url = (os.getenv("OCR_BASE_URL") or "http://localhost:8080").strip().rstrip("/")
    endpoint = (os.getenv("OCR_ENDPOINT") or "").strip()
    field = (os.getenv("OCR_FIELD_NAME") or "file").strip()
    try:
        timeout = float(os.getenv("OCR_TIMEOUT") or 60)
    except ValueError:
        timeout = 60.0

    if endpoint and not endpoint.startswith("/"):
        endpoint = "/" + endpoint

    return {
        "mode": mode,
        # --- chế độ llm ---
        "model": (os.getenv("OCR_MODEL") or "gpt-4o").strip(),
        "api_key": (os.getenv("OPENAI_API_KEY") or "").strip(),
        # --- chế độ http ---
        "base_url": base_url,
        "endpoint": endpoint,
        "field": field,
        "timeout": timeout,
    }


def _endpoints_to_try(cfg: dict) -> list:
    return [cfg["endpoint"]] if cfg["endpoint"] else list(CANDIDATE_ENDPOINTS)


def _fields_to_try(cfg: dict) -> list:
    fields = [cfg["field"]]
    fields += [f for f in CANDIDATE_FIELDS if f != cfg["field"]]
    return fields


# =============================================================================
# KIỂM TRA SẴN SÀNG
# =============================================================================

def check_ocr_connection() -> dict:
    """
    Kiểm tra OCR đã sẵn sàng chưa (tuỳ chế độ).

    Returns:
        dict: {"ok": bool, "message": str, "mode": str}
    """
    cfg = get_config()

    # ---------- Chế độ LLM Vision ----------
    if cfg["mode"] == "llm":
        if not cfg["api_key"] or cfg["api_key"].startswith("your_"):
            return {"ok": False, "mode": "llm",
                    "message": ("Chưa cấu hình OPENAI_API_KEY trong .env — "
                                "OCR bằng LLM Vision không hoạt động được.")}
        try:
            import openai  # noqa: F401
        except ImportError:
            return {"ok": False, "mode": "llm",
                    "message": "Chưa cài thư viện openai. Chạy: pip install openai"}
        return {"ok": True, "mode": "llm",
                "message": f"Sẵn sàng OCR bằng LLM Vision (model: {cfg['model']})."}

    # ---------- Chế độ HTTP ----------
    try:
        res = requests.get(cfg["base_url"], timeout=min(cfg["timeout"], 5))
        return {"ok": True, "mode": "http",
                "message": f"Kết nối được tới {cfg['base_url']} (HTTP {res.status_code})."}
    except requests.exceptions.ConnectTimeout:
        return {"ok": False, "mode": "http",
                "message": f"Hết thời gian chờ khi kết nối tới {cfg['base_url']}."}
    except requests.exceptions.ConnectionError:
        return {"ok": False, "mode": "http",
                "message": (f"Không kết nối được tới {cfg['base_url']}. Kiểm tra: "
                            f"(1) máy chạy OCR đã bật chưa, (2) IP trong OCR_BASE_URL "
                            f"có đúng không, (3) firewall có chặn cổng không, "
                            f"(4) hai máy có cùng mạng LAN không.")}
    except Exception as e:
        return {"ok": False, "mode": "http",
                "message": f"Lỗi không xác định khi ping: {type(e).__name__}: {e}"}


# =============================================================================
# TRÍCH XUẤT TRƯỜNG TỪ TEXT THÔ
# =============================================================================

_RE_INVOICE_NO = [
    re.compile(r"(?:số\s*(?:hóa\s*đơn|hd|hđ)|invoice\s*(?:no|number)|no\.)\s*[:\-]?\s*([A-Z0-9\-/]{3,25})", re.I),
    re.compile(r"\b(HD[\-\s]?\d{4}[\-\s]?\d{3,6})\b", re.I),
]
_RE_TAX_CODE = [
    re.compile(r"(?:mã\s*số\s*thuế|mst|tax\s*code|tax\s*id)\s*[:\-]?\s*(\d{10}(?:[\-\s]?\d{3})?)", re.I),
    re.compile(r"\b(\d{10}-\d{3})\b"),
]
_RE_DATE = [
    re.compile(r"(?:ngày|date)\s*[:\-]?\s*(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{4})", re.I),
    re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"),
    re.compile(r"\b(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{4})\b"),
]
_RE_AMOUNT = [
    re.compile(r"(?:tổng\s*cộng|tổng\s*tiền\s*thanh\s*toán|thành\s*tiền|tổng\s*thanh\s*toán|total)\s*[:\-]?\s*([\d.,]{4,20})", re.I),
    re.compile(r"([\d.,]{4,20})\s*(?:đ|vnđ|vnd)\b", re.I),
]
_RE_VAT = [
    re.compile(r"(?:tiền\s*thuế\s*gtgt|thuế\s*gtgt|vat)\s*[:\-]?\s*([\d.,]{3,20})", re.I),
]
_RE_VENDOR = [
    re.compile(r"(?:đơn\s*vị\s*bán\s*hàng|người\s*bán|nhà\s*cung\s*cấp|tên\s*đơn\s*vị|seller|vendor)\s*[:\-]?\s*(.+)", re.I),
]


def _first_match(patterns, text):
    for pat in patterns:
        m = pat.search(text)
        if m:
            return m.group(1).strip()
    return ""


def parse_amount(raw) -> float:
    """
    Chuyển chuỗi tiền tệ Việt Nam về số.

    '4.500.000' -> 4500000.0 ; '4,500,000' -> 4500000.0 ; '1.234.567,89' -> 1234567.89
    Không parse được -> 0.0 (KHÔNG raise, KHÔNG đoán bừa).
    """
    if raw is None or raw == "":
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)

    s = re.sub(r"[^\d.,]", "", str(raw)).strip()
    if not s:
        return 0.0

    n_dot, n_comma = s.count("."), s.count(",")

    if n_dot and n_comma:
        # Có CẢ hai loại dấu ➔ dấu xuất hiện SAU CÙNG là dấu thập phân,
        # loại còn lại chắc chắn là dấu phân cách hàng nghìn.
        if s.rfind(".") > s.rfind(","):
            s = s.replace(",", "")                      # 1,234.56
        else:
            s = s.replace(".", "").replace(",", ".")    # 1.234,56
    elif n_dot:
        # CHỈ có dấu chấm. Nhiều hơn 1 dấu ➔ chắc chắn là phân cách hàng nghìn
        # (4.950.000). Đúng 1 dấu mà phần đuôi 3 chữ số cũng là hàng nghìn (850.000)
        # — kiểu Việt Nam hầu như không viết 2 chữ số thập phân cho VNĐ.
        head, tail = s.rsplit(".", 1)
        if n_dot > 1 or len(tail) == 3:
            s = s.replace(".", "")
    elif n_comma:
        # Tương tự cho dấu phẩy: 4,950,000 hoặc 850,000
        head, tail = s.rsplit(",", 1)
        if n_comma > 1 or len(tail) == 3:
            s = s.replace(",", "")
        else:
            s = s.replace(",", ".")                     # 1234,5 -> thập phân

    try:
        return float(s)
    except ValueError:
        return 0.0


def normalize_date(raw: str) -> str:
    """Chuẩn hoá ngày về 'YYYY-MM-DD'. Không nhận dạng được -> chuỗi rỗng."""
    if not raw:
        return ""
    raw = str(raw).strip()

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw

    m = re.fullmatch(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})", raw)
    if m:
        d, mth, y = m.groups()
        return f"{y}-{int(mth):02d}-{int(d):02d}"
    return ""


def extract_fields_from_text(text: str) -> dict:
    """Trích 6 trường nghiệp vụ từ text thô."""
    if not text:
        return {}

    vendor = _first_match(_RE_VENDOR, text)
    if vendor:
        vendor = vendor.splitlines()[0].strip(" :-\t")[:100]

    return {
        "invoice_no": _first_match(_RE_INVOICE_NO, text),
        "vendor": vendor,
        "tax_code": _first_match(_RE_TAX_CODE, text).replace(" ", "").replace("-", ""),
        "invoice_date": normalize_date(_first_match(_RE_DATE, text)),
        "amount": parse_amount(_first_match(_RE_AMOUNT, text)),
        "vat": parse_amount(_first_match(_RE_VAT, text)),
    }


_FIELD_ALIASES = {
    "invoice_no": ["invoice_no", "invoice_number", "invoiceNo", "so_hoa_don", "number", "no"],
    "vendor": ["vendor", "seller", "supplier", "seller_name", "nha_cung_cap", "ten_don_vi"],
    "tax_code": ["tax_code", "taxCode", "tax_id", "mst", "ma_so_thue", "seller_tax_code"],
    "invoice_date": ["invoice_date", "date", "issue_date", "ngay", "ngay_lap"],
    "amount": ["amount", "total", "total_amount", "grand_total", "tong_tien", "thanh_tien"],
    "vat": ["vat", "tax", "vat_amount", "tien_thue", "thue_gtgt"],
}

_TEXT_KEYS = ["text", "ocr_text", "raw_text", "content", "full_text", "result_text"]


def normalize_ocr_result(raw) -> dict:
    """
    Chuẩn hoá phản hồi OCR về đúng 6 trường nghiệp vụ.

    Xử lý được 3 kiểu đầu vào:
      1. dict có sẵn các trường  ➔ dùng luôn (LLM Vision trả về dạng này)
      2. dict chỉ có text thô    ➔ regex trích xuất
      3. chuỗi text/plain        ➔ regex trích xuất
    """
    result = {"invoice_no": "", "vendor": "", "tax_code": "",
              "invoice_date": "", "amount": 0.0, "vat": 0.0, "raw_text": ""}

    if raw is None:
        result["missing_fields"] = _find_missing(result)
        return result

    if isinstance(raw, str):
        result["raw_text"] = raw
        result.update({k: v for k, v in extract_fields_from_text(raw).items() if v})
        result["missing_fields"] = _find_missing(result)
        return result

    if not isinstance(raw, dict):
        result["raw_text"] = str(raw)
        result["missing_fields"] = _find_missing(result)
        return result

    # Một số nguồn bọc kết quả trong 'data' / 'result' / 'prediction'
    payload = raw
    for wrapper in ("data", "result", "results", "prediction", "output"):
        inner = payload.get(wrapper)
        if isinstance(inner, dict):
            payload = {**payload, **inner}
        elif isinstance(inner, list) and inner and isinstance(inner[0], dict):
            payload = {**payload, **inner[0]}

    for field, aliases in _FIELD_ALIASES.items():
        for alias in aliases:
            if alias in payload and payload[alias] not in (None, "", "null"):
                value = payload[alias]
                if field in ("amount", "vat"):
                    result[field] = parse_amount(value)
                elif field == "invoice_date":
                    result[field] = normalize_date(str(value))
                else:
                    result[field] = str(value).strip()
                break

    # Bù các trường còn thiếu từ text thô nếu có
    text = ""
    for key in _TEXT_KEYS:
        if isinstance(payload.get(key), str) and payload[key].strip():
            text = payload[key]
            break
    if not text:
        for key in ("lines", "texts", "blocks"):
            val = payload.get(key)
            if isinstance(val, list) and val:
                parts = [v if isinstance(v, str) else str(v.get("text", "")) for v in val]
                text = "\n".join(p for p in parts if p)
                break

    result["raw_text"] = text
    if text:
        for field, value in extract_fields_from_text(text).items():
            if not result.get(field):
                result[field] = value

    result["missing_fields"] = _find_missing(result)
    return result


def _find_missing(data: dict) -> list:
    """Liệt kê các trường bắt buộc còn thiếu (Điều 1, QD-TC-02/2026)."""
    missing = []
    for field, label in [("invoice_no", "số hóa đơn"), ("vendor", "tên nhà cung cấp"),
                         ("tax_code", "mã số thuế"), ("invoice_date", "ngày hóa đơn")]:
        if not data.get(field):
            missing.append(label)
    if not data.get("amount"):
        missing.append("tổng tiền")
    return missing


# =============================================================================
# ĐƯỜNG DẪN ẢNH
# =============================================================================

def resolve_image_path(image_path: str) -> str:
    """'hd_001.jpg' và 'data/invoices/hd_001.jpg' đều tìm ra cùng một file."""
    if not image_path:
        return ""
    image_path = image_path.strip().strip('"').strip("'")

    if os.path.isabs(image_path) and os.path.exists(image_path):
        return image_path

    for candidate in (
        os.path.join(BASE_DIR, image_path),
        os.path.join(INVOICE_DIR, os.path.basename(image_path)),
        image_path,
    ):
        if os.path.exists(candidate):
            return candidate
    return ""


def list_invoice_images() -> list:
    """Liệt kê ảnh hóa đơn có trong data/invoices/ (bỏ qua README.md)."""
    if not os.path.isdir(INVOICE_DIR):
        return []
    return sorted(f for f in os.listdir(INVOICE_DIR) if f.lower().endswith(SUPPORTED_EXT))


# =============================================================================
# 🤖 OCR BẰNG LLM VISION (GPT-4o) — CHẾ ĐỘ MẶC ĐỊNH
# =============================================================================

OCR_VISION_PROMPT = """Bạn là hệ thống trích xuất dữ liệu hóa đơn của phòng kế toán.

Nhìn kỹ ảnh hóa đơn và trích xuất CHÍNH XÁC các trường sau, trả về DUY NHẤT một object JSON:

{
  "invoice_no":   "số/ký hiệu hóa đơn, ví dụ 'HD-2026-0017' hoặc '00012345'",
  "vendor":       "tên đầy đủ đơn vị BÁN HÀNG (người bán), không phải người mua",
  "tax_code":     "mã số thuế của NGƯỜI BÁN, chỉ gồm chữ số, 10 hoặc 13 số",
  "invoice_date": "ngày lập hóa đơn, định dạng YYYY-MM-DD",
  "amount":       "TỔNG TIỀN THANH TOÁN cuối cùng, chỉ gồm chữ số, không dấu chấm phẩy",
  "vat":          "tiền thuế GTGT, chỉ gồm chữ số",
  "raw_text":     "toàn bộ chữ đọc được trên hóa đơn"
}

QUY TẮC BẮT BUỘC:
1. Trường nào KHÔNG đọc được hoặc không có trên hóa đơn thì để chuỗi rỗng "".
   TUYỆT ĐỐI KHÔNG suy đoán, không ước lượng, không bịa. Kế toán sẽ dùng số này để
   chi tiền thật — một con số sai gây thiệt hại trực tiếp.
2. "amount" phải là TỔNG CỘNG cuối cùng (đã gồm thuế), KHÔNG phải "cộng tiền hàng"
   hay đơn giá của một dòng hàng.
3. "vendor" là bên BÁN. Nếu hóa đơn ghi cả bên mua và bên bán, lấy đúng bên bán.
4. "tax_code" lấy MST của bên bán, bỏ mọi dấu gạch ngang và khoảng trắng.
5. Chỉ trả về JSON, không giải thích gì thêm."""


def _image_to_data_uri(path: str) -> str:
    """Đọc file ảnh thành data URI base64 để nhúng vào request vision."""
    ext = os.path.splitext(path)[1].lower()
    mime = MIME_TYPES.get(ext, "image/jpeg")
    with open(path, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def call_ocr_llm(image_path: str, cfg: dict) -> dict:
    """
    Nhận dạng hóa đơn bằng LLM đa phương thức (GPT-4o Vision).

    Trả về dict đã chuẩn hoá, hoặc {"ok": False, "error": "LỖI OCR: ..."}.
    """
    if not cfg["api_key"] or cfg["api_key"].startswith("your_"):
        return {"ok": False,
                "error": ("LỖI OCR: Chưa cấu hình OPENAI_API_KEY trong file .env nên không "
                          "dùng được OCR bằng LLM Vision. Hãy điền API key, hoặc chuyển sang "
                          "OCR_MODE=http nếu có service OCR nội bộ.")}

    ext = os.path.splitext(image_path)[1].lower()
    if ext not in VISION_EXT:
        return {"ok": False,
                "error": (f"LỖI OCR: Chế độ LLM Vision không đọc được định dạng '{ext}'. "
                          f"Chỉ nhận: {', '.join(VISION_EXT)}. "
                          f"Với file PDF, hãy chuyển sang ảnh trước hoặc dùng OCR_MODE=http.")}

    try:
        import openai
    except ImportError:
        return {"ok": False,
                "error": "LỖI OCR: Chưa cài thư viện openai. Chạy: pip install openai"}

    try:
        data_uri = _image_to_data_uri(image_path)
    except OSError as e:
        return {"ok": False, "error": f"LỖI OCR: Không đọc được file ảnh — {e}"}

    try:
        client = openai.OpenAI(api_key=cfg["api_key"], timeout=cfg["timeout"])
        response = client.chat.completions.create(
            model=cfg["model"],
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": OCR_VISION_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_uri, "detail": "high"}},
                ],
            }],
            response_format={"type": "json_object"},
            temperature=0,      # nhiệt độ 0 ➔ kết quả ổn định giữa các lần chạy
            max_tokens=2000,
        )
        content = response.choices[0].message.content
    except Exception as e:
        return {"ok": False,
                "error": (f"LỖI OCR: Gọi {cfg['model']} thất bại — {type(e).__name__}: {e}. "
                          f"Kiểm tra API key, hạn mức sử dụng và kết nối mạng.")}

    # --- Parse JSON model trả về ---
    try:
        parsed_json = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        # Model không tuân thủ JSON ➔ vớt bằng regex trên text thô
        block = re.search(r"\{.*\}", str(content or ""), re.DOTALL)
        if block:
            try:
                parsed_json = json.loads(block.group(0))
            except json.JSONDecodeError:
                parsed_json = str(content)
        else:
            parsed_json = str(content)

    data = normalize_ocr_result(parsed_json)
    data["ok"] = True
    data["engine"] = f"llm-vision ({cfg['model']})"
    data["source_image"] = os.path.relpath(image_path, BASE_DIR).replace("\\", "/")
    return data


# =============================================================================
# 🌐 OCR BẰNG SERVICE HTTP — CHẾ ĐỘ DỰ PHÒNG
# =============================================================================

def call_ocr_http(image_path: str, cfg: dict) -> dict:
    """Gửi ảnh tới service OCR chạy ở máy khác trong LAN, tự dò endpoint và field."""
    last_error = ""

    for endpoint in _endpoints_to_try(cfg):
        url = cfg["base_url"] + endpoint
        for field in _fields_to_try(cfg):
            try:
                with open(image_path, "rb") as fh:
                    res = requests.post(url, files={field: (os.path.basename(image_path), fh)},
                                        timeout=cfg["timeout"])
            except requests.exceptions.ConnectionError:
                return {"ok": False,
                        "error": (f"LỖI OCR: Không kết nối được tới service OCR tại "
                                  f"{cfg['base_url']}. Kiểm tra máy chạy OCR đã bật chưa, "
                                  f"IP trong OCR_BASE_URL có đúng không, và firewall có "
                                  f"chặn cổng không.")}
            except requests.exceptions.Timeout:
                return {"ok": False,
                        "error": (f"LỖI OCR: Service OCR tại {cfg['base_url']} không phản hồi "
                                  f"sau {cfg['timeout']:.0f} giây.")}
            except OSError as e:
                return {"ok": False, "error": f"LỖI OCR: Không đọc được file ảnh — {e}"}
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                continue

            if res.status_code == 200:
                try:
                    raw = res.json()
                except ValueError:
                    raw = res.text
                data = normalize_ocr_result(raw)
                data["ok"] = True
                data["engine"] = f"http ({url})"
                data["source_image"] = os.path.relpath(image_path, BASE_DIR).replace("\\", "/")
                return data

            if res.status_code in (404, 405):
                last_error = f"HTTP {res.status_code} tại {url}"
                continue    # endpoint sai -> thử endpoint kế tiếp
            if res.status_code in (400, 422):
                last_error = f"HTTP {res.status_code} tại {url} (field '{field}' có thể sai)"
                continue    # field sai -> thử field kế tiếp

            last_error = f"HTTP {res.status_code} tại {url}: {res.text[:200]}"

    tried = ", ".join(_endpoints_to_try(cfg))
    return {"ok": False,
            "error": (f"LỖI OCR: Service tại {cfg['base_url']} không xử lý được ảnh. "
                      f"Đã thử các endpoint [{tried}]. Lỗi cuối: {last_error}. "
                      f"Hãy khai báo đúng đường dẫn vào biến OCR_ENDPOINT trong file .env.")}


# =============================================================================
# ĐIỂM VÀO CHUNG
# =============================================================================

def call_ocr(image_path: str) -> dict:
    """
    Nhận dạng hóa đơn từ ảnh, tự chọn engine theo OCR_MODE.

    Returns:
        Thành công -> {ok: True, invoice_no, vendor, tax_code, invoice_date,
                       amount, vat, missing_fields, engine, source_image, raw_text}
        Thất bại   -> {ok: False, error: "LỖI OCR: ..."}

    ⚠️ KHÔNG BAO GIỜ raise — mọi sự cố đều thành chuỗi lỗi để Agent suy luận tiếp.
    """
    resolved = resolve_image_path(image_path)
    if not resolved:
        available = list_invoice_images()
        hint = (f"Các file hiện có trong data/invoices/: {', '.join(available)}."
                if available else
                "Thư mục data/invoices/ đang TRỐNG — hãy chép ảnh hóa đơn vào đó trước.")
        return {"ok": False,
                "error": f"LỖI OCR: Không tìm thấy file ảnh '{image_path}'. {hint}"}

    ext = os.path.splitext(resolved)[1].lower()
    if ext not in SUPPORTED_EXT:
        return {"ok": False,
                "error": (f"LỖI OCR: Định dạng '{ext}' không được hỗ trợ. "
                          f"Chỉ nhận: {', '.join(SUPPORTED_EXT)}.")}

    cfg = get_config()
    if cfg["mode"] == "llm":
        return call_ocr_llm(resolved, cfg)
    return call_ocr_http(resolved, cfg)


# =============================================================================
# SMOKE TEST
# =============================================================================

if __name__ == "__main__":
    print("=" * 72)
    print("📷 KIỂM TRA OCR CLIENT")
    print("=" * 72)

    cfg = get_config()
    print(f"\n⚙️  Cấu hình hiện tại:")
    print(f"   OCR_MODE   = {cfg['mode']}")
    if cfg["mode"] == "llm":
        key = cfg["api_key"]
        masked = (key[:7] + "..." + key[-4:]) if len(key) > 12 else "(chưa điền)"
        print(f"   OCR_MODEL  = {cfg['model']}")
        print(f"   API key    = {masked}")
    else:
        print(f"   OCR_BASE_URL   = {cfg['base_url']}")
        print(f"   OCR_ENDPOINT   = {cfg['endpoint'] or '(để trống - sẽ tự dò)'}")
        print(f"   OCR_FIELD_NAME = {cfg['field']}")
    print(f"   OCR_TIMEOUT = {cfg['timeout']}s")

    print("\n--- 🔌 KIỂM TRA SẴN SÀNG ---")
    state = check_ocr_connection()
    print(f"   {'✅' if state['ok'] else '❌'} {state['message']}")

    print("\n--- 📁 ẢNH HÓA ĐƠN CÓ SẴN ---")
    images = list_invoice_images()
    if images:
        for img in images:
            print(f"   • {img}")
    else:
        print("   (trống) Hãy chép ảnh hóa đơn vào data/invoices/")

    print("\n--- 🧪 TEST PARSER TEXT THÔ (không cần gọi API) ---")
    sample = """
    HÓA ĐƠN GIÁ TRỊ GIA TĂNG
    Số hóa đơn: HD-2026-0099
    Ngày: 15/07/2026
    Đơn vị bán hàng: Nhà hàng Sen Tây Hồ
    Mã số thuế: 0101245789
    Thuế GTGT: 450.000
    Tổng cộng: 4.950.000 đ
    """
    parsed = normalize_ocr_result(sample)
    for key in ("invoice_no", "vendor", "tax_code", "invoice_date", "amount", "vat"):
        print(f"   {key:<14}: {parsed[key]}")
    print(f"   {'thiếu':<14}: {parsed['missing_fields'] or 'không thiếu trường nào'}")

    print("\n--- 🧪 TEST PARSER JSON (dạng LLM Vision trả về) ---")
    parsed2 = normalize_ocr_result({
        "invoice_no": "HD-2026-0100", "vendor": "FPT Shop", "tax_code": "0101248141",
        "invoice_date": "2026-07-20", "amount": "8900000", "vat": "890000",
    })
    print(f"   {parsed2['invoice_no']} | {parsed2['vendor']} | {parsed2['tax_code']} | "
          f"{parsed2['invoice_date']} | {parsed2['amount']:,.0f}")

    print("\n--- 🧪 TEST TRƯỜNG THIẾU (model để trống, KHÔNG bịa) ---")
    parsed3 = normalize_ocr_result({
        "invoice_no": "", "vendor": "Quán ăn ABC", "tax_code": "",
        "invoice_date": "2026-07-20", "amount": "", "vat": "",
    })
    print(f"   thiếu: {parsed3['missing_fields']}")

    print("\n--- ❌ TEST FILE KHÔNG TỒN TẠI ---")
    print("  ", call_ocr("data/invoices/khong_ton_tai.jpg")["error"])

    if images and state["ok"]:
        print(f"\n--- 🚀 OCR THẬT VỚI '{images[0]}' ---")
        real = call_ocr(images[0])
        if real.get("ok"):
            print(f"   ✅ Engine: {real['engine']}")
            for key in ("invoice_no", "vendor", "tax_code", "invoice_date", "amount", "vat"):
                print(f"   {key:<14}: {real[key]}")
            print(f"   thiếu         : {real['missing_fields'] or 'không thiếu'}")
            if real.get("raw_text"):
                print(f"\n   --- Text đọc được (200 ký tự đầu) ---")
                print("   " + real["raw_text"][:200].replace("\n", "\n   "))
        else:
            print(f"   ❌ {real['error']}")

    print("\n✅ ocr_client.py hoạt động bình thường.")
