"""
📚 RAG — TRA CỨU CHÍNH SÁCH NỘI BỘ (BM25 thuần Python)

Vì sao KHÔNG dùng vector database (Chroma/FAISS)?
  • Kho tri thức chỉ ~30 điều khoản — BM25 cho kết quả tốt tương đương mà nhanh hơn.
  • Không thêm dependency nặng ➔ giảng viên chấm chéo `pip install -r requirements.txt`
    là chạy được ngay trên máy Windows bất kỳ.
  • Kết quả DETERMINISTIC ➔ demo trước lớp không bị "lúc ra lúc không".
  • Không tốn API call embedding ➔ không phụ thuộc mạng.

Điểm mấu chốt về GROUNDING: mỗi đoạn trả về đều kèm TRÍCH DẪN NGUỒN
`[Nguồn: <tên file> § <tên điều>]` để Final Answer của Agent có bằng chứng
kiểm chứng được, thay vì "nghe có vẻ đúng".

Chạy độc lập:  python src/rag.py
"""

import math
import os
import re
import sys
import unicodedata

if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POLICY_DIR = os.path.join(BASE_DIR, "data", "policies")

# Tham số BM25 chuẩn
BM25_K1 = 1.5
BM25_B = 0.75

# Ngưỡng lọc kết quả rác.
# Không có ngưỡng, truy vấn lạc đề ("cách nấu phở bò") vẫn trả về điều khoản
# ngẫu nhiên chỉ vì trùng vài từ vặt ➔ Agent tưởng là bằng chứng và trích dẫn sai.
#   • MIN_ABSOLUTE_SCORE: điểm tối thiểu để coi là thực sự liên quan
#   • MIN_RELATIVE_RATIO: kết quả phụ phải đạt ít nhất X% điểm của kết quả đầu
MIN_ABSOLUTE_SCORE = 8.0
MIN_RELATIVE_RATIO = 0.30

# Từ dừng tiếng Việt — loại bớt nhiễu khi tính điểm
STOPWORDS = {
    "là", "của", "và", "các", "có", "được", "cho", "khi", "này", "đó", "một",
    "những", "với", "trong", "để", "từ", "theo", "về", "tại", "đến", "hoặc",
    "phải", "không", "thì", "mà", "nếu", "sẽ", "đã", "bị", "bởi", "vào", "ra",
    "tôi", "bạn", "mình", "gì", "nào", "sao", "thế", "ạ", "nhé", "ah", "à",
    "the", "a", "an", "of", "is", "are", "to", "for", "in", "on", "and", "or",
}


# =============================================================================
# TIỀN XỬ LÝ VĂN BẢN
# =============================================================================

def strip_accents(text: str) -> str:
    """
    Bỏ dấu tiếng Việt: 'hạn mức' -> 'han muc'.

    Dùng để người dùng gõ không dấu vẫn tìm được ('han muc tiep khach').
    Xử lý riêng chữ đ/Đ vì unicodedata không tách được.
    """
    text = text.replace("đ", "d").replace("Đ", "D")
    nfkd = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in nfkd if unicodedata.category(ch) != "Mn")


def tokenize(text: str) -> list:
    """
    Tách từ đơn giản: lowercase ➔ bỏ dấu câu ➔ tách khoảng trắng ➔ bỏ stopword.

    Mỗi token được index ở CẢ HAI dạng (có dấu + không dấu) để truy vấn
    không dấu vẫn khớp được với văn bản có dấu.
    """
    text = text.lower()
    text = re.sub(r"[^\w\sÀ-ỹ]", " ", text, flags=re.UNICODE)
    raw = [t for t in text.split() if t and t not in STOPWORDS and len(t) > 1]

    tokens = []
    for t in raw:
        tokens.append(t)
        no_accent = strip_accents(t)
        if no_accent != t:
            tokens.append(no_accent)
    return tokens


# =============================================================================
# NẠP & CẮT ĐOẠN KHO TRI THỨC
# =============================================================================

def load_chunks(policy_dir: str = POLICY_DIR) -> list:
    """
    Đọc toàn bộ file .md trong data/policies/ và cắt thành các chunk theo heading '## '.

    Mỗi Điều trong văn bản = 1 chunk độc lập ➔ trả về đúng điều khoản liên quan
    thay vì cả file dài.

    Returns:
        list[dict]: [{doc, section, text, tokens}, ...]
    """
    chunks = []
    if not os.path.isdir(policy_dir):
        return chunks

    for filename in sorted(os.listdir(policy_dir)):
        if not filename.lower().endswith(".md"):
            continue

        path = os.path.join(policy_dir, filename)
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError:
            continue

        # Cắt theo heading cấp 2 ('## Điều 1 — ...')
        parts = re.split(r"^##\s+", content, flags=re.MULTILINE)

        # parts[0] là phần đầu file (tiêu đề + mã văn bản) — giữ làm 1 chunk giới thiệu
        header = parts[0].strip()
        if header:
            title = header.splitlines()[0].lstrip("# ").strip()
            chunks.append({
                "doc": filename,
                "section": "Thông tin văn bản",
                "text": header,
                "tokens": tokenize(header),
            })

        for part in parts[1:]:
            lines = part.strip().splitlines()
            if not lines:
                continue
            section = lines[0].strip()
            body = "\n".join(lines[1:]).strip()
            full = f"{section}\n{body}"
            chunks.append({
                "doc": filename,
                "section": section,
                "text": full,
                "tokens": tokenize(full),
            })

    return chunks


# =============================================================================
# BM25
# =============================================================================

class BM25Index:
    """
    Chỉ mục BM25 (Okapi). Xây một lần khi khởi động, tra cứu nhiều lần.

    Công thức:
        score(D, Q) = Σ IDF(q) · f(q,D)·(k1+1) / (f(q,D) + k1·(1 − b + b·|D|/avgdl))
        IDF(q)      = ln( (N − n(q) + 0.5) / (n(q) + 0.5) + 1 )
    """

    def __init__(self, chunks: list):
        self.chunks = chunks
        self.N = len(chunks)
        self.doc_freq = {}      # token -> số chunk chứa token
        self.term_freq = []     # [ {token: số lần xuất hiện}, ... ]
        self.doc_len = []
        self.avgdl = 0.0
        self._build()

    def _build(self):
        for chunk in self.chunks:
            tokens = chunk["tokens"]
            self.doc_len.append(len(tokens))

            tf = {}
            for t in tokens:
                tf[t] = tf.get(t, 0) + 1
            self.term_freq.append(tf)

            for t in tf:
                self.doc_freq[t] = self.doc_freq.get(t, 0) + 1

        self.avgdl = (sum(self.doc_len) / self.N) if self.N else 0.0

    def _idf(self, token: str) -> float:
        n = self.doc_freq.get(token, 0)
        return math.log((self.N - n + 0.5) / (n + 0.5) + 1)

    def search(self, query: str, top_k: int = 3, apply_threshold: bool = True) -> list:
        """
        Tìm top_k chunk liên quan nhất.

        Args:
            apply_threshold: True = lọc bỏ kết quả rác theo MIN_ABSOLUTE_SCORE
                             và MIN_RELATIVE_RATIO. Đặt False khi muốn xem điểm thô.

        Returns:
            list[dict]: [{score, doc, section, text}, ...] đã sắp giảm dần theo điểm.
        """
        q_tokens = tokenize(query)
        if not q_tokens or self.N == 0:
            return []

        scored = []
        for i, chunk in enumerate(self.chunks):
            tf = self.term_freq[i]
            dl = self.doc_len[i] or 1
            score = 0.0
            for qt in q_tokens:
                f = tf.get(qt, 0)
                if f == 0:
                    continue
                denom = f + BM25_K1 * (1 - BM25_B + BM25_B * dl / (self.avgdl or 1))
                score += self._idf(qt) * (f * (BM25_K1 + 1) / denom)

            if score > 0:
                scored.append({
                    "score": round(score, 4),
                    "doc": chunk["doc"],
                    "section": chunk["section"],
                    "text": chunk["text"],
                })

        scored.sort(key=lambda x: x["score"], reverse=True)

        if apply_threshold and scored:
            top_score = scored[0]["score"]
            if top_score < MIN_ABSOLUTE_SCORE:
                return []   # Cả truy vấn lạc đề ➔ thà trả rỗng còn hơn trả bằng chứng sai
            cutoff = top_score * MIN_RELATIVE_RATIO
            scored = [s for s in scored if s["score"] >= cutoff]

        return scored[:top_k]


# =============================================================================
# API CÔNG KHAI
# =============================================================================

_INDEX = None


def get_index() -> BM25Index:
    """Lấy chỉ mục BM25 (lazy — chỉ xây ở lần gọi đầu tiên)."""
    global _INDEX
    if _INDEX is None:
        _INDEX = BM25Index(load_chunks())
    return _INDEX


def reload_index() -> int:
    """Nạp lại kho tri thức sau khi sửa file policy. Trả về số chunk."""
    global _INDEX
    _INDEX = BM25Index(load_chunks())
    return _INDEX.N


def search_policy_chunks(query: str, top_k: int = 3) -> list:
    """Tra cứu thô — trả về list dict, dùng cho Streamlit hoặc xử lý tiếp."""
    return get_index().search(query, top_k=top_k)


def format_citation(chunk: dict) -> str:
    """Sinh chuỗi trích dẫn nguồn: '[Nguồn: 01_han_muc_chi_tieu.md § Điều 3 — ...]'"""
    return f"[Nguồn: {chunk['doc']} § {chunk['section']}]"


def search_policy_text(query: str, top_k: int = 3, max_chars: int = 700) -> str:
    """
    Tra cứu và trả về CHUỖI đã định dạng sẵn cho Agent đọc (kèm trích dẫn nguồn).

    Đây là hàm mà tool `search_policy` trong tools.py gọi tới.
    Không tìm thấy -> trả chuỗi hướng dẫn, KHÔNG raise.
    """
    if not query or not query.strip():
        return "LỖI: Cần cung cấp từ khoá hoặc câu hỏi để tra cứu chính sách."

    index = get_index()
    if index.N == 0:
        return (f"LỖI: Kho chính sách trống. Kiểm tra lại thư mục {POLICY_DIR} "
                f"đã có các file .md chưa.")

    results = index.search(query, top_k=top_k)
    if not results:
        available = sorted({c["doc"] for c in index.chunks})
        return (f"Không tìm thấy điều khoản nào khớp với '{query}'.\n"
                f"Các văn bản hiện có trong kho chính sách: {', '.join(available)}.\n"
                f"Gợi ý: thử từ khoá cụ thể hơn như 'hạn mức tiếp khách', "
                f"'mã số thuế', 'ngưỡng phê duyệt', 'phân quyền nhân viên'.")

    lines = [f"Tìm thấy {len(results)} điều khoản liên quan đến '{query}':\n"]
    for i, r in enumerate(results, start=1):
        body = r["text"]
        if len(body) > max_chars:
            body = body[:max_chars].rsplit(" ", 1)[0] + "..."
        lines.append(f"--- Kết quả {i} (độ liên quan {r['score']}) ---")
        lines.append(format_citation(r))
        lines.append(body)
        lines.append("")
    return "\n".join(lines).strip()


# =============================================================================
# SMOKE TEST
# =============================================================================

if __name__ == "__main__":
    print("=" * 70)
    print("📚 KIỂM TRA RAG — TRA CỨU CHÍNH SÁCH NỘI BỘ (BM25)")
    print("=" * 70)

    n = reload_index()
    index = get_index()
    docs = sorted({c["doc"] for c in index.chunks})
    print(f"\n✅ Đã nạp {n} đoạn (chunk) từ {len(docs)} văn bản:")
    for d in docs:
        cnt = sum(1 for c in index.chunks if c["doc"] == d)
        print(f"   • {d:<35} ({cnt} điều)")

    queries = [
        "hạn mức tiếp khách là bao nhiêu",
        "han muc tiep khach",                      # gõ KHÔNG DẤU -> vẫn phải ra
        "mã số thuế trên hóa đơn có bắt buộc không",
        "ngưỡng nào bắt buộc phải có người duyệt",
        "nhân viên có được chuyển khoản không",
        "ảnh hóa đơn bị mờ không đọc được thì làm sao",
    ]

    for q in queries:
        print("\n" + "=" * 70)
        print(f"❓ TRUY VẤN: {q}")
        print("-" * 70)
        for r in search_policy_chunks(q, top_k=2):
            preview = r["text"].replace("\n", " ")[:150]
            print(f"  [{r['score']:>6}] {format_citation(r)}")
            print(f"           {preview}...")

    print("\n" + "=" * 70)
    print("❌ TEST TRUY VẤN KHÔNG KHỚP GÌ")
    print("-" * 70)
    print(search_policy_text("cách nấu phở bò gia truyền"))

    print("\n✅ rag.py hoạt động bình thường.")
