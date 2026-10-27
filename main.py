"""
Dịch vụ cắt hình cho hệ thống Ngân hàng câu hỏi.

POST /extract   (header X-Api-Secret)
{
  "mime": "application/pdf" | "image/png" | "image/jpeg" | "image/webp",
  "data": "<base64 của file gốc>",
  "gemini_key": "<khóa Gemini>",
  "items": [ {"name": "hinh_x.png", "page": 3, "desc": "hình tam giác ABC bài 2"} ]
}
-> {"results": [ {"name": "...", "ok": true, "png_b64": "..."} | {"name": "...", "ok": false, "error": "..."} ]}

Cách làm: render trang PDF thành ảnh -> gửi ảnh trang cho Gemini để lấy khung bao quanh hình
(box_2d chuẩn hóa 0-1000) -> cắt bằng Pillow -> tỉa viền trắng -> trả PNG.
"""
import base64
import hmac
import io
import json
import os
import time
import threading
import subprocess
import tempfile
import shutil

import requests
from flask import Flask, jsonify, request
from PIL import Image, ImageChops

app = Flask(__name__)

API_SECRET = os.environ.get("API_SECRET", "")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
DPI = int(os.environ.get("RENDER_DPI", "150"))   # 150 để vừa gói miễn phí 512 MB RAM
SCAN_PAGES = int(os.environ.get("SCAN_PAGES", "12"))   # khi không rõ số trang: dò tối đa bấy nhiêu trang
PAD = 0.015                                             # nới khung 1,5% mỗi phía để không mất nhãn điểm A, B, C
MIN_SIDE = 40                                           # ảnh cắt nhỏ hơn mức này coi như thất bại

PROMPT = (
    "Đây là một trang tài liệu Toán. Hãy tìm HÌNH VẼ / ĐỒ THỊ / BẢNG BIẾN THIÊN / HÌNH MINH HỌA khớp với mô tả sau:\n"
    "\"{desc}\"\n"
    "Trả về JSON đúng dạng {{\"box_2d\": [ymin, xmin, ymax, xmax]}} với toạ độ chuẩn hóa 0-1000 trên trang này. "
    "Khung phải bao trọn hình cùng các nhãn điểm/số liệu thuộc hình (A, B, C, trục toạ độ, chú thích ngắn), "
    "nhưng KHÔNG gồm đoạn văn đề bài ở ngoài hình. "
    "Nếu trang không có hình khớp mô tả, trả {{\"box_2d\": null}}."
)


# ---------- Gemini ----------
def ask_box(page_png: bytes, desc: str, key: str):
    """Trả về (ymin, xmin, ymax, xmax) chuẩn hóa 0-1000 hoặc None nếu trang không có hình."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    body = {
        "contents": [{
            "role": "user",
            "parts": [
                {"text": PROMPT.format(desc=desc)},
                {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(page_png).decode()}},
            ],
        }],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    last = ""
    for attempt in range(4):
        r = requests.post(url, headers={"x-goog-api-key": key}, json=body, timeout=90)
        if r.status_code == 200:
            cand = (r.json().get("candidates") or [{}])[0]
            text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []))
            return parse_box(text)
        last = f"Gemini HTTP {r.status_code}: {r.text[:200]}"
        if r.status_code in (400, 401, 403, 404):
            raise RuntimeError(last)
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(last)


def parse_box(text: str):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):] if "{" in text else text
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(data, list):
        data = data[0] if data else {}
    box = data.get("box_2d") if isinstance(data, dict) else None
    if not box or len(box) != 4:
        return None
    try:
        ymin, xmin, ymax, xmax = [float(v) for v in box]
    except (TypeError, ValueError):
        return None
    if ymax <= ymin or xmax <= xmin:
        return None
    return ymin, xmin, ymax, xmax


# ---------- Hình ảnh ----------
def crop(page_img: Image.Image, box):
    w, h = page_img.size
    ymin, xmin, ymax, xmax = box
    x0 = max(0.0, xmin / 1000 - PAD) * w
    x1 = min(1.0, xmax / 1000 + PAD) * w
    y0 = max(0.0, ymin / 1000 - PAD) * h
    y1 = min(1.0, ymax / 1000 + PAD) * h
    img = page_img.crop((int(x0), int(y0), int(x1), int(y1))).convert("RGB")
    return trim_white(img)


def trim_white(img: Image.Image, margin: int = 10):
    bg = Image.new("RGB", img.size, (255, 255, 255))
    bbox = ImageChops.difference(img, bg).point(lambda v: 255 if v > 18 else 0).getbbox()
    if not bbox:
        return img
    l, t, r, b = bbox
    l, t = max(0, l - margin), max(0, t - margin)
    r, b = min(img.width, r + margin), min(img.height, b + margin)
    return img.crop((l, t, r, b))


def to_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class Source:
    """Nguồn trang: PDF (render từng trang) hoặc một ảnh đơn. Chỉ giữ trang gần nhất trong RAM."""

    def __init__(self, mime: str, raw: bytes):
        self.last = None   # (số trang, ảnh, png)
        if mime == "application/pdf":
            import fitz  # PyMuPDF, nhập muộn để phần ảnh đơn chạy được khi chưa cài
            self.doc = fitz.open(stream=raw, filetype="pdf")
            self.n = self.doc.page_count
            self.single = None
        else:
            self.doc = None
            self.single = Image.open(io.BytesIO(raw)).convert("RGB")
            self.n = 1

    def page(self, p: int):
        """Trả về (ảnh, png_bytes) của trang p (đếm từ 1)."""
        if self.last and self.last[0] == p:
            return self.last[1], self.last[2]
        if self.single is not None:
            img = self.single
        else:
            pix = self.doc[p - 1].get_pixmap(dpi=DPI)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            del pix
        png = to_png(img)
        self.last = (p, img, png)
        return img, png


def extract_one(src: Source, item: dict, key: str) -> bytes:
    desc = str(item.get("desc") or "hình minh họa")
    try:
        page = int(item.get("page") or 0)
    except (TypeError, ValueError):
        page = 0

    # Thử đúng trang Gemini đã ghi; nếu sai/không rõ thì dò lần lượt các trang gần đó.
    order = []
    if 1 <= page <= src.n:
        order.append(page)
        order += [p for p in (page - 1, page + 1) if 1 <= p <= src.n]
    else:
        order = list(range(1, min(src.n, SCAN_PAGES) + 1))

    for p in order:
        img, png = src.page(p)
        box = ask_box(png, desc, key)
        if not box:
            continue
        out = crop(img, box)
        if out.width >= MIN_SIDE and out.height >= MIN_SIDE:
            return to_png(out)
    raise RuntimeError("Không tìm thấy hình khớp mô tả")

# ---------- Chia PDF thành đoạn trang (cho hệ thống Ngân hàng câu hỏi) ----------
_CACHE = {"key": None, "doc": None, "path": None}
_LOCK = threading.Lock()
MAX_CHUNK_BYTES = 13 * 1024 * 1024   # đoạn nặng hơn mức này không gửi inline cho Gemini được
COMPRESS_LEVELS = [(150, 75), (120, 65)]   # (dpi đích, chất lượng JPEG), thử lần lượt khi đoạn quá nặng


def docx_to_pdf_bytes(raw: bytes) -> bytes:
    """Đổi DOCX sang PDF bằng LibreOffice, hoàn toàn trong thư mục tạm."""
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "in.docx")
        with open(src, "wb") as fh:
            fh.write(raw)
        r = subprocess.run(
            ["soffice", f"-env:UserInstallation=file://{td}/lo_profile",
             "--headless", "--norestore", "--nolockcheck",
             "--convert-to", "pdf:writer_pdf_Export", "--outdir", td, src],
            capture_output=True, timeout=240,
        )
        out = os.path.join(td, "in.pdf")
        if not os.path.exists(out):
            raise RuntimeError("LibreOffice không tạo được PDF: " + r.stderr.decode(errors="ignore")[:200])
        with open(out, "rb") as fh:
            return fh.read()


def drive_download_to_file(file_id: str, token: str, path: str):
    """Tải từng khối 1 MB thẳng xuống đĩa, không giữ cả file trong RAM."""
    with requests.get(
        f"https://www.googleapis.com/drive/v3/files/{file_id}",
        params={"alt": "media", "supportsAllDrives": "true"},
        headers={"Authorization": f"Bearer {token}"},
        stream=True, timeout=(30, 600),
    ) as r:
        if r.status_code != 200:
            raise RuntimeError(f"Drive HTTP {r.status_code}: {r.text[:200]}")
        with open(path, "wb") as fh:
            for part in r.iter_content(1024 * 1024):
                if part:
                    fh.write(part)


def _drop_cache():
    if _CACHE["doc"] is not None:
        try:
            _CACHE["doc"].close()
        except Exception:
            pass
    if _CACHE["path"]:
        try:
            os.remove(_CACHE["path"])
        except OSError:
            pass
    _CACHE.update(key=None, doc=None, path=None)


def open_cached(file_id: str, ver: str, token: str):
    """Tải file một lần (DOCX thì đổi sang PDF), các đoạn sau dùng lại không tải nữa."""
    import fitz
    key = (file_id, ver)
    if _CACHE["key"] != key:
        _drop_cache()
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        try:
            drive_download_to_file(file_id, token, path)
            with open(path, "rb") as fh:
                head = fh.read(2)
            if head == b"PK":                      # DOCX là file zip; PDF bắt đầu bằng %PDF
                with open(path, "rb") as fh:
                    raw = fh.read()
                pdf = docx_to_pdf_bytes(raw)
                del raw
                with open(path, "wb") as fh:
                    fh.write(pdf)
                del pdf
            doc = fitz.open(path, filetype="pdf")  # đọc từ đĩa, không nạp hết vào RAM
        except Exception:
            try:
                os.remove(path)
            except OSError:
                pass
            raise
        _CACHE.update(key=key, doc=doc, path=path)
    return _CACHE["doc"]


def build_chunk(src, start: int, end: int):
    """Cắt trang start..end thành PDF nhỏ; nếu quá nặng thì nén ảnh. Trả (bytes, mô tả mức nén hoặc None)."""
    import fitz
    out = fitz.open()
    try:
        out.insert_pdf(src, from_page=start - 1, to_page=end - 1)
        data = out.tobytes(garbage=3, deflate=True)
        reduced = None
        if len(data) > MAX_CHUNK_BYTES and hasattr(out, "rewrite_images"):
            for dpi, q in COMPRESS_LEVELS:
                out.rewrite_images(dpi_threshold=dpi + 20, dpi_target=dpi, quality=q)
                data = out.tobytes(garbage=3, deflate=True)
                reduced = f"{dpi}dpi/q{q}"
                if len(data) <= MAX_CHUNK_BYTES:
                    break
        return data, reduced
    finally:
        out.close()


@app.post("/chunk")
def chunk():
    if not API_SECRET or not hmac.compare_digest(request.headers.get("X-Api-Secret", ""), API_SECRET):
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(force=True, silent=True) or {}
    file_id, token, ver = body.get("file_id"), body.get("token"), str(body.get("ver") or "")
    if not (file_id and token):
        return jsonify({"error": "thiếu file_id/token"}), 400
    try:
        start, end = int(body.get("from") or 1), int(body.get("to") or 1)
    except (TypeError, ValueError):
        return jsonify({"error": "from/to không hợp lệ"}), 400
    try:
        with _LOCK:
            src = open_cached(file_id, ver, token)
            n = src.page_count
            start, end = max(1, start), min(end, n)
            if start > end:
                return jsonify({"n_pages": n, "from": start, "to": end, "data": ""})
            data, reduced = build_chunk(src, start, end)
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500
    if len(data) > MAX_CHUNK_BYTES:
        return jsonify({"error": f"đoạn trang {start}-{end} nặng {len(data) // 1048576} MB, quá lớn"}), 413
    if reduced:
        print(f"Đã nén ảnh đoạn {start}-{end} ({reduced}) còn {len(data) // 1024} KB", flush=True)
    return jsonify({"n_pages": n, "from": start, "to": end, "data": base64.b64encode(data).decode()})
# ---------- HTTP ----------
@app.get("/")
def health():
    return "ok"


@app.post("/extract")
def extract():
    if not API_SECRET or not hmac.compare_digest(request.headers.get("X-Api-Secret", ""), API_SECRET):
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(force=True, silent=True) or {}
    key, mime, data, items = body.get("gemini_key"), body.get("mime"), body.get("data"), body.get("items") or []
    if not (key and mime and data and items):
        return jsonify({"error": "thiếu gemini_key/mime/data/items"}), 400
    try:
        src = Source(mime, base64.b64decode(data))
    except Exception as e:  # file hỏng hoặc định dạng không hỗ trợ
        return jsonify({"error": f"không mở được file: {e}"}), 400

    results = []
    for it in items:
        name = it.get("name", "")
        try:
            png = extract_one(src, it, key)
            results.append({"name": name, "ok": True, "png_b64": base64.b64encode(png).decode()})
        except Exception as e:
            results.append({"name": name, "ok": False, "error": str(e)[:300]})
    return jsonify({"results": results})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
