# 📷 THƯ MỤC ẢNH HÓA ĐƠN

> ⚠️ **Ảnh trong thư mục này KHÔNG được commit lên Git** (đã cấu hình trong `.gitignore`).
> Hóa đơn thật chứa tên nhà cung cấp, mã số thuế, số tiền — đây là **PII / dữ liệu nhạy cảm của doanh nghiệp**.

---

## Cách sử dụng

1. Chép ảnh hóa đơn (`.jpg`, `.jpeg`, `.png`, `.pdf`) vào **chính thư mục này**.
2. Kiểm tra Agent nhìn thấy file chưa:
   ```bash
   python src/tools.py
   ```
   Tool `list_invoice_files` sẽ liệt kê ra toàn bộ ảnh có ở đây.
3. Chạy Agent xử lý hóa đơn:
   ```bash
   python src/app.py --role ketoan --query "Xử lý và thanh toán hóa đơn data/invoices/hd_001.jpg"
   ```

## Quy ước đặt tên (khuyến nghị)

```
hd_<số thứ tự>_<nhà cung cấp viết tắt>.jpg
```

Ví dụ: `hd_001_vietnamairlines.jpg` · `hd_002_nhahangsen.jpg` · `hd_003_fptshop.jpg`

Không bắt buộc — Agent đọc được mọi tên file, nhưng đặt tên rõ ràng giúp bạn dễ đối chiếu khi xem log trace.

## Định dạng được hỗ trợ

| Đuôi file | Trạng thái |
| :--- | :--- |
| `.jpg` / `.jpeg` | ✅ |
| `.png` | ✅ |
| `.webp` | ✅ |
| `.pdf` | ⚠️ Tùy service OCR ở máy `OCR_BASE_URL` có hỗ trợ hay không |

## Lưu ý khi demo

- Cần **ít nhất 1 ảnh** trong thư mục này thì Test Case #5 và #9 mới chạy được.
- Nếu thư mục rỗng, tool `list_invoice_files` trả về thông báo hướng dẫn thay vì lỗi — Agent sẽ báo lại cho bạn chứ không tự bịa tên file.
- Service OCR phải đang chạy tại địa chỉ khai báo ở biến `OCR_BASE_URL` trong file `.env`.
