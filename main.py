import os
import uuid
import shutil
import subprocess
import json
import base64
from pathlib import Path
from typing import Optional

import anthropic
import fitz
from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import uvicorn

app = FastAPI(title="File Converter API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Original-Size", "X-Compressed-Size", "X-Reduction"],
)

UPLOAD_DIR = Path("/tmp/uploads")
OUTPUT_DIR = Path("/tmp/outputs")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tiff"}

def save_upload(file: UploadFile) -> Path:
    ext  = Path(file.filename).suffix.lower()
    dest = UPLOAD_DIR / f"{uuid.uuid4()}{ext}"
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    return dest

def cleanup(*paths: Path):
    for p in paths:
        try:
            if p and p.exists(): p.unlink()
        except: pass

def is_image_ext(ext: str) -> bool:
    return ext.lstrip(".") in IMAGE_EXTS

@app.get("/")
async def health():
    return {"status": "ok", "service": "File Converter API"}

@app.post("/api/ai/process")
async def ai_process(
    file: UploadFile = File(...),
    tool: str = Form(...),
    options: Optional[str] = Form(None),
):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(500, "ANTHROPIC_API_KEY chưa được cấu hình")

    opts = json.loads(options) if options else {}
    path = save_upload(file)
    ext  = path.suffix.lower()

    try:
        client = anthropic.Anthropic(api_key=api_key)
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()

        media_map = {
            ".pdf": "application/pdf", ".png": "image/png",
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".webp": "image/webp",
        }
        media_type = media_map.get(ext, "application/pdf")
        is_image   = is_image_ext(ext)

        if tool == "extract":
            prompt = "Extract all text from this document. Preserve headings, paragraphs, lists, and tables."
        elif tool == "translate":
            lang = opts.get("lang", "vi")
            lang_names = {"vi":"Vietnamese (tiếng Việt)","en":"English","ja":"Japanese (日本語)","zh":"Chinese (中文)","fr":"French (Français)","ko":"Korean (한국어)","de":"German (Deutsch)","es":"Spanish (Español)"}
            prompt = f"Translate all text to {lang_names.get(lang, lang)}. Return only translated text."
        elif tool == "summarize":
            fmt = opts.get("format", "bullet")
            prompt = "Summarize as clear bullet points." if fmt == "bullet" else "Write a concise paragraph summary (3–5 sentences)."
        elif tool == "qa":
            question = opts.get("question", "What is this document about?")
            prompt = f"Answer this question based on the document:\n\n{question}"
        elif tool == "ocr":
            prompt = "Perform OCR. Extract ALL visible text exactly as it appears."
        else:
            raise HTTPException(400, f"Tool không hợp lệ: {tool}")

        content = (
            [{"type":"image",    "source":{"type":"base64","media_type":media_type,        "data":b64}}, {"type":"text","text":prompt}]
            if is_image else
            [{"type":"document", "source":{"type":"base64","media_type":"application/pdf", "data":b64}}, {"type":"text","text":prompt}]
        )

        response = client.messages.create(
            model="claude-sonnet-4-5", max_tokens=4096,
            messages=[{"role":"user","content":content}],
        )
        result_text = "".join(b.text for b in response.content if hasattr(b, "text"))
        return {"success": True, "result": result_text}
    finally:
        cleanup(path)

@app.post("/api/convert/pdf-to-word")
async def pdf_to_word(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Chỉ chấp nhận file PDF")
    path     = save_upload(file)
    out_path = OUTPUT_DIR / f"{uuid.uuid4()}.docx"
    try:
        from pdf2docx import Converter
        cv = Converter(str(path))
        cv.convert(str(out_path), start=0, end=None)
        cv.close()
        if not out_path.exists():
            raise HTTPException(500, "Không tạo được file DOCX")
        return FileResponse(str(out_path),
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename=Path(file.filename).stem + ".docx")
    finally:
        cleanup(path)

@app.post("/api/convert/word-to-pdf")
async def word_to_pdf(file: UploadFile = File(...)):
    ext = Path(file.filename).suffix.lower()
    if ext not in {".doc", ".docx"}:
        raise HTTPException(400, "Chỉ chấp nhận .doc hoặc .docx")
    path     = save_upload(file)
    out_path = OUTPUT_DIR / f"{uuid.uuid4()}.pdf"
    try:
        lo = "/usr/bin/libreoffice"
        if Path(lo).exists():
            r = subprocess.run([lo, "--headless", "--convert-to", "pdf",
                "--outdir", str(OUTPUT_DIR), str(path)],
                capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                raise HTTPException(500, f"LibreOffice lỗi: {r.stderr}")
            out_path = OUTPUT_DIR / (path.stem + ".pdf")
        else:
            from docx2pdf import convert
            convert(str(path), str(out_path))
        if not out_path.exists():
            raise HTTPException(500, "Không tạo được PDF")
        return FileResponse(str(out_path), media_type="application/pdf",
            filename=Path(file.filename).stem + ".pdf")
    except subprocess.TimeoutExpired:
        raise HTTPException(500, "Timeout")
    finally:
        cleanup(path)

@app.post("/api/convert/pdf-to-image")
async def pdf_to_image(file: UploadFile = File(...), page: int = Form(0), dpi: int = Form(150), fmt: str = Form("png")):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Chỉ chấp nhận file PDF")
    path     = save_upload(file)
    out_path = OUTPUT_DIR / f"{uuid.uuid4()}.{fmt}"
    try:
        doc = fitz.open(str(path))
        if page >= len(doc): raise HTTPException(400, f"PDF chỉ có {len(doc)} trang")
        pix = doc[page].get_pixmap(matrix=fitz.Matrix(dpi/72, dpi/72))
        pix.save(str(out_path))
        doc.close()
        return FileResponse(str(out_path),
            media_type="image/png" if fmt=="png" else "image/jpeg",
            filename=f"{Path(file.filename).stem}_page{page+1}.{fmt}")
    finally:
        cleanup(path)

@app.post("/api/convert/merge-pdf")
async def merge_pdf(files: list[UploadFile] = File(...)):
    if len(files) < 2: raise HTTPException(400, "Cần ít nhất 2 file PDF")
    paths = []; out_path = OUTPUT_DIR / f"{uuid.uuid4()}.pdf"
    try:
        for f in files:
            if not f.filename.lower().endswith(".pdf"): raise HTTPException(400, f"'{f.filename}' không phải PDF")
            paths.append(save_upload(f))
        merged = fitz.open()
        for p in paths:
            doc = fitz.open(str(p)); merged.insert_pdf(doc); doc.close()
        merged.save(str(out_path)); merged.close()
        return FileResponse(str(out_path), media_type="application/pdf", filename="merged.pdf")
    finally:
        for p in paths: cleanup(p)

@app.post("/api/convert/split-pdf")
async def split_pdf(file: UploadFile = File(...), pages: str = Form(...)):
    if not file.filename.lower().endswith(".pdf"): raise HTTPException(400, "Chỉ chấp nhận file PDF")
    path = save_upload(file); out_path = OUTPUT_DIR / f"{uuid.uuid4()}.pdf"
    try:
        doc = fitz.open(str(path)); total = len(doc)
        page_nums = set()
        for part in pages.split(","):
            part = part.strip()
            if not part: continue
            if "-" in part:
                s, e = part.split("-", 1)
                for n in range(int(s), int(e)+1): page_nums.add(n-1)
            else: page_nums.add(int(part)-1)
        invalid = [n+1 for n in page_nums if n < 0 or n >= total]
        if invalid: raise HTTPException(400, f"Trang không tồn tại: {invalid}")
        new_doc = fitz.open()
        for n in sorted(page_nums): new_doc.insert_pdf(doc, from_page=n, to_page=n)
        new_doc.save(str(out_path)); new_doc.close(); doc.close()
        return FileResponse(str(out_path), media_type="application/pdf",
            filename=f"{Path(file.filename).stem}_split.pdf")
    finally:
        cleanup(path)

@app.post("/api/convert/compress-pdf")
async def compress_pdf(file: UploadFile = File(...), level: str = Form("medium")):
    if not file.filename.lower().endswith(".pdf"): raise HTTPException(400, "Chỉ chấp nhận file PDF")
    path = save_upload(file); out_path = OUTPUT_DIR / f"{uuid.uuid4()}.pdf"
    gs_quality = {"low":"/screen","medium":"/ebook","high":"/printer"}.get(level,"/ebook")
    try:
        r = subprocess.run(["gs","-sDEVICE=pdfwrite","-dCompatibilityLevel=1.4",
            f"-dPDFSETTINGS={gs_quality}","-dNOPAUSE","-dQUIET","-dBATCH",
            f"-sOutputFile={out_path}",str(path)],
            capture_output=True, text=True, timeout=120)
        if r.returncode != 0: raise HTTPException(500, f"Ghostscript lỗi: {r.stderr}")
        orig = path.stat().st_size; comp = out_path.stat().st_size
        resp = FileResponse(str(out_path), media_type="application/pdf",
            filename=f"{Path(file.filename).stem}_compressed.pdf")
        resp.headers["X-Original-Size"]   = str(orig)
        resp.headers["X-Compressed-Size"] = str(comp)
        resp.headers["X-Reduction"]       = str(round((1-comp/orig)*100,1))
        return resp
    except FileNotFoundError:
        raise HTTPException(500, "Ghostscript chưa cài trên server")
    finally:
        cleanup(path)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
