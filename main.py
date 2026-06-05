import os, uuid, shutil, json, base64
from pathlib import Path
from typing import Optional

import anthropic
import fitz
from payment import router as payment_router
import httpx
from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import uvicorn

app = FastAPI(title="File Converter API")

app.include_router(payment_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Original-Size","X-Compressed-Size","X-Reduction","X-Plan","X-Usage-Today","X-Usage-Limit"],
)

UPLOAD_DIR = Path("/tmp/uploads")
OUTPUT_DIR = Path("/tmp/outputs")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

SUPABASE_URL     = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE = os.environ.get("SUPABASE_SERVICE_KEY", "")
IMAGE_EXTS       = {"png","jpg","jpeg","gif","webp","bmp","tiff"}

PLAN_LIMITS = {"free": 5, "basic": 100, "pro": -1}


# ── Helpers ────────────────────────────────────────────────────────────────────

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


# ── Auth helpers ───────────────────────────────────────────────────────────────

async def get_current_user(authorization: Optional[str] = Header(None)) -> dict:
    """Verify Supabase JWT, trả về user dict."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Cần đăng nhập để sử dụng tính năng này")
    token = authorization.split(" ", 1)[1]
    async with httpx.AsyncClient() as client:
        res = await client.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={"Authorization": f"Bearer {token}", "apikey": SUPABASE_SERVICE},
            timeout=5,
        )
    if res.status_code != 200:
        raise HTTPException(401, "Phiên đăng nhập hết hạn, vui lòng đăng nhập lại")
    return res.json()


async def check_and_log_usage(user: dict, tool: str) -> dict:
    """Kiểm tra quota và ghi log. Trả về info plan."""
    uid = user["id"]
    async with httpx.AsyncClient() as client:
        # Lấy plan
        pr = await client.get(
            f"{SUPABASE_URL}/rest/v1/profiles?id=eq.{uid}&select=plan",
            headers={"Authorization": f"Bearer {SUPABASE_SERVICE}", "apikey": SUPABASE_SERVICE},
            timeout=5,
        )
        profiles = pr.json()
        plan  = profiles[0].get("plan", "free") if profiles else "free"
        limit = PLAN_LIMITS.get(plan, 5)

        if limit != -1:
            # Đếm usage hôm nay
            ur = await client.post(
                f"{SUPABASE_URL}/rest/v1/rpc/get_usage_today",
                headers={"Authorization": f"Bearer {SUPABASE_SERVICE}", "apikey": SUPABASE_SERVICE, "Content-Type": "application/json"},
                json={"p_user_id": uid},
                timeout=5,
            )
            used = ur.json() or 0
            if used >= limit:
                raise HTTPException(429, f"Đã dùng hết {limit} lần/ngày (plan {plan}). Nâng cấp để tiếp tục.")
        else:
            used = 0

        # Ghi usage
        await client.post(
            f"{SUPABASE_URL}/rest/v1/usage",
            headers={"Authorization": f"Bearer {SUPABASE_SERVICE}", "apikey": SUPABASE_SERVICE,
                     "Content-Type": "application/json", "Prefer": "return=minimal"},
            json={"user_id": uid, "tool": tool},
            timeout=5,
        )

    return {"plan": plan, "limit": limit, "used": used + 1}


# ── Health check ───────────────────────────────────────────────────────────────

@app.get("/")
async def health():
    return {"status": "ok", "service": "File Converter API v2"}


@app.post("/api/auth/set-plan")
async def set_plan(
    plan: str = Form(...),
    user: dict = Depends(get_current_user),
):
    """Endpoint để test — đổi plan của user."""
    uid = user["id"]
    if plan not in ("free", "basic", "pro"):
        raise HTTPException(400, "Plan không hợp lệ")
    async with httpx.AsyncClient() as client:
        await client.patch(
            f"{SUPABASE_URL}/rest/v1/profiles?id=eq.{uid}",
            headers={
                "Authorization": f"Bearer {SUPABASE_SERVICE}",
                "apikey": SUPABASE_SERVICE,
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
            json={"plan": plan},
            timeout=5,
        )
    return {"success": True, "plan": plan}


# ── Auth endpoints ─────────────────────────────────────────────────────────────

@app.get("/api/auth/me")
async def get_me(user: dict = Depends(get_current_user)):
    """Trả về thông tin user hiện tại + plan + usage."""
    uid = user["id"]
    async with httpx.AsyncClient() as client:
        pr = await client.get(
            f"{SUPABASE_URL}/rest/v1/profiles?id=eq.{uid}&select=plan,full_name,email,avatar_url",
            headers={"Authorization": f"Bearer {SUPABASE_SERVICE}", "apikey": SUPABASE_SERVICE},
            timeout=5,
        )
        ur = await client.post(
            f"{SUPABASE_URL}/rest/v1/rpc/get_usage_today",
            headers={"Authorization": f"Bearer {SUPABASE_SERVICE}", "apikey": SUPABASE_SERVICE, "Content-Type": "application/json"},
            json={"p_user_id": uid},
            timeout=5,
        )
    profiles = pr.json()
    profile  = profiles[0] if profiles else {}
    plan     = profile.get("plan", "free")
    used     = ur.json() or 0
    limit    = PLAN_LIMITS.get(plan, 5)

    return {
        "id":         uid,
        "email":      profile.get("email") or user.get("email"),
        "full_name":  profile.get("full_name"),
        "avatar_url": profile.get("avatar_url"),
        "plan":       plan,
        "usage_today": used,
        "usage_limit": limit,
    }


# ── AI: phân tích file ─────────────────────────────────────────────────────────

@app.post("/api/ai/process")
async def ai_process(
    file: UploadFile = File(...),
    tool: str = Form(...),
    options: Optional[str] = Form(None),
    user: dict = Depends(get_current_user),
):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(500, "ANTHROPIC_API_KEY chưa được cấu hình")

    # Check + log usage
    usage_info = await check_and_log_usage(user, tool)

    opts = json.loads(options) if options else {}
    path = save_upload(file)
    ext  = path.suffix.lower()

    try:
        client = anthropic.Anthropic(api_key=api_key)
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()

        media_map = {".pdf":"application/pdf",".png":"image/png",".jpg":"image/jpeg",
                     ".jpeg":"image/jpeg",".gif":"image/gif",".webp":"image/webp"}
        media_type = media_map.get(ext, "application/pdf")
        is_image   = is_image_ext(ext)

        if tool == "extract":
            prompt = "Extract all text from this document. Preserve headings, paragraphs, lists, and tables."
        elif tool == "translate":
            lang = opts.get("lang", "vi")
            lang_names = {"vi":"Vietnamese (tiếng Việt)","en":"English","ja":"Japanese (日本語)",
                          "zh":"Chinese (中文)","fr":"French (Français)","ko":"Korean (한국어)",
                          "de":"German (Deutsch)","es":"Spanish (Español)"}
            prompt = f"Translate all text to {lang_names.get(lang,lang)}. Return only translated text."
        elif tool == "summarize":
            fmt = opts.get("format","bullet")
            prompt = "Summarize as clear bullet points." if fmt=="bullet" else "Write a concise paragraph summary (3–5 sentences)."
        elif tool == "qa":
            question = opts.get("question","What is this document about?")
            prompt = f"Answer this question based on the document:\n\n{question}"
        elif tool == "ocr":
            prompt = "Perform OCR. Extract ALL visible text exactly as it appears."
        else:
            raise HTTPException(400, f"Tool không hợp lệ: {tool}")

        content = (
            [{"type":"image",    "source":{"type":"base64","media_type":media_type,"data":b64}},
             {"type":"text","text":prompt}]
            if is_image else
            [{"type":"document", "source":{"type":"base64","media_type":"application/pdf","data":b64}},
             {"type":"text","text":prompt}]
        )

        response = client.messages.create(
            model="claude-sonnet-4-5", max_tokens=4096,
            messages=[{"role":"user","content":content}],
        )
        result_text = "".join(b.text for b in response.content if hasattr(b,"text"))

        res = {"success": True, "result": result_text}
        res.update({"plan": usage_info["plan"], "usage_today": usage_info["used"], "usage_limit": usage_info["limit"]})
        return res

    finally:
        cleanup(path)


# ── PDF → Word ─────────────────────────────────────────────────────────────────

@app.post("/api/convert/pdf-to-word")
async def pdf_to_word(
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Chỉ chấp nhận file PDF")

    await check_and_log_usage(user, "pdf2word")
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


# ── Word → PDF ─────────────────────────────────────────────────────────────────

@app.post("/api/convert/word-to-pdf")
async def word_to_pdf(
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    ext = Path(file.filename).suffix.lower()
    if ext not in {".doc", ".docx"}:
        raise HTTPException(400, "Chỉ chấp nhận .doc hoặc .docx")

    await check_and_log_usage(user, "word2pdf")
    path     = save_upload(file)
    out_path = OUTPUT_DIR / f"{uuid.uuid4()}.pdf"
    try:
        lo = "/usr/bin/libreoffice"
        if Path(lo).exists():
            import subprocess
            r = subprocess.run([lo, "--headless", "--convert-to", "pdf",
                "--outdir", str(OUTPUT_DIR), str(path)],
                capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                raise HTTPException(500, f"Lỗi convert: {r.stderr}")
            out_path = OUTPUT_DIR / (path.stem + ".pdf")
        else:
            from docx2pdf import convert
            convert(str(path), str(out_path))
        if not out_path.exists():
            raise HTTPException(500, "Không tạo được PDF")
        return FileResponse(str(out_path), media_type="application/pdf",
            filename=Path(file.filename).stem + ".pdf")
    finally:
        cleanup(path)


# ── PDF → Image ────────────────────────────────────────────────────────────────

@app.post("/api/convert/pdf-to-image")
async def pdf_to_image(
    file: UploadFile = File(...),
    page: int = Form(0),
    dpi:  int = Form(150),
    fmt:  str = Form("png"),
    user: dict = Depends(get_current_user),
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Chỉ chấp nhận file PDF")
    path     = save_upload(file)
    out_path = OUTPUT_DIR / f"{uuid.uuid4()}.{fmt}"
    try:
        doc = fitz.open(str(path))
        if page >= len(doc): raise HTTPException(400, f"PDF chỉ có {len(doc)} trang")
        pix = doc[page].get_pixmap(matrix=fitz.Matrix(dpi/72,dpi/72))
        pix.save(str(out_path)); doc.close()
        return FileResponse(str(out_path),
            media_type="image/png" if fmt=="png" else "image/jpeg",
            filename=f"{Path(file.filename).stem}_page{page+1}.{fmt}")
    finally:
        cleanup(path)


# ── Merge PDF ──────────────────────────────────────────────────────────────────

@app.post("/api/convert/merge-pdf")
async def merge_pdf(
    files: list[UploadFile] = File(...),
    user: dict = Depends(get_current_user),
):
    if len(files) < 2: raise HTTPException(400, "Cần ít nhất 2 file PDF")
    paths = []; out_path = OUTPUT_DIR / f"{uuid.uuid4()}.pdf"
    try:
        for f in files:
            if not f.filename.lower().endswith(".pdf"):
                raise HTTPException(400, f"'{f.filename}' không phải PDF")
            paths.append(save_upload(f))
        merged = fitz.open()
        for p in paths:
            doc = fitz.open(str(p)); merged.insert_pdf(doc); doc.close()
        merged.save(str(out_path)); merged.close()
        return FileResponse(str(out_path), media_type="application/pdf", filename="merged.pdf")
    finally:
        for p in paths: cleanup(p)


# ── Split PDF ──────────────────────────────────────────────────────────────────

@app.post("/api/convert/split-pdf")
async def split_pdf(
    file: UploadFile = File(...),
    pages: str = Form(...),
    user: dict = Depends(get_current_user),
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Chỉ chấp nhận file PDF")
    path = save_upload(file); out_path = OUTPUT_DIR / f"{uuid.uuid4()}.pdf"
    try:
        doc = fitz.open(str(path)); total = len(doc)
        page_nums = set()
        for part in pages.split(","):
            part = part.strip()
            if not part: continue
            if "-" in part:
                s, e = part.split("-",1)
                for n in range(int(s),int(e)+1): page_nums.add(n-1)
            else: page_nums.add(int(part)-1)
        invalid = [n+1 for n in page_nums if n<0 or n>=total]
        if invalid: raise HTTPException(400, f"Trang không tồn tại: {invalid}")
        new_doc = fitz.open()
        for n in sorted(page_nums): new_doc.insert_pdf(doc, from_page=n, to_page=n)
        new_doc.save(str(out_path)); new_doc.close(); doc.close()
        return FileResponse(str(out_path), media_type="application/pdf",
            filename=f"{Path(file.filename).stem}_split.pdf")
    finally:
        cleanup(path)


# ── Compress PDF ───────────────────────────────────────────────────────────────

@app.post("/api/convert/compress-pdf")
async def compress_pdf(
    file: UploadFile = File(...),
    level: str = Form("medium"),
    user: dict = Depends(get_current_user),
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Chỉ chấp nhận file PDF")
    path = save_upload(file); out_path = OUTPUT_DIR / f"{uuid.uuid4()}.pdf"
    try:
        doc = fitz.open(str(path))
        doc.save(str(out_path), garbage=4, deflate=True, clean=True)
        doc.close()
        orig = path.stat().st_size; comp = out_path.stat().st_size
        resp = FileResponse(str(out_path), media_type="application/pdf",
            filename=f"{Path(file.filename).stem}_compressed.pdf")
        resp.headers["X-Original-Size"]   = str(orig)
        resp.headers["X-Compressed-Size"] = str(comp)
        resp.headers["X-Reduction"]       = str(round((1-comp/orig)*100,1))
        return resp
    finally:
        cleanup(path)


# ── Admin endpoints ───────────────────────────────────────────────────────────

def check_admin(secret: str):
    if secret != os.environ.get("ADMIN_SECRET", "change-me-secret"):
        raise HTTPException(403, "Không có quyền truy cập")

@app.get("/admin")
async def admin_panel():
    from fastapi.responses import HTMLResponse
    admin_html = Path("admin.html")
    if admin_html.exists():
        return HTMLResponse(admin_html.read_text(encoding="utf-8"))
    raise HTTPException(404, "Admin panel không tìm thấy")

@app.get("/api/admin/orders")
async def admin_get_orders(secret: str = ""):
    check_admin(secret)
    async with httpx.AsyncClient() as client:
        res = await client.get(
            f"{SUPABASE_URL}/rest/v1/payment_orders?order=created_at.desc&limit=100"
            f"&select=id,user_id,user_email,plan,amount,method,status,created_at,confirmed_at",
            headers={"Authorization": f"Bearer {SUPABASE_SERVICE}", "apikey": SUPABASE_SERVICE},
            timeout=5,
        )
    return res.json()

@app.post("/api/admin/set-plan")
async def admin_set_plan(
    email:        str = Form(...),
    plan:         str = Form(...),
    admin_secret: str = Form(...),
):
    check_admin(admin_secret)
    if plan not in ("free", "basic", "pro"):
        raise HTTPException(400, "Plan không hợp lệ")

    async with httpx.AsyncClient() as client:
        # Lấy user_id từ email
        res = await client.get(
            f"{SUPABASE_URL}/rest/v1/profiles?email=eq.{email}&select=id",
            headers={"Authorization": f"Bearer {SUPABASE_SERVICE}", "apikey": SUPABASE_SERVICE},
            timeout=5,
        )
        profiles = res.json()
        if not profiles:
            raise HTTPException(404, f"Không tìm thấy user: {email}")

        uid = profiles[0]["id"]
        await client.patch(
            f"{SUPABASE_URL}/rest/v1/profiles?id=eq.{uid}",
            headers={"Authorization": f"Bearer {SUPABASE_SERVICE}", "apikey": SUPABASE_SERVICE,
                     "Content-Type": "application/json", "Prefer": "return=minimal"},
            json={"plan": plan},
            timeout=5,
        )

    return {"success": True, "email": email, "plan": plan}

@app.get("/api/admin/stats")
async def admin_stats(secret: str = ""):
    check_admin(secret)
    async with httpx.AsyncClient() as client:
        users_res = await client.get(
            f"{SUPABASE_URL}/rest/v1/profiles?select=plan",
            headers={"Authorization": f"Bearer {SUPABASE_SERVICE}", "apikey": SUPABASE_SERVICE},
            timeout=5,
        )
        orders_res = await client.get(
            f"{SUPABASE_URL}/rest/v1/payment_orders?status=eq.completed&select=amount",
            headers={"Authorization": f"Bearer {SUPABASE_SERVICE}", "apikey": SUPABASE_SERVICE},
            timeout=5,
        )

    users  = users_res.json()
    orders = orders_res.json()

    plan_count = {"free": 0, "basic": 0, "pro": 0}
    for u in users:
        plan_count[u.get("plan", "free")] = plan_count.get(u.get("plan","free"), 0) + 1

    total_revenue = sum(o["amount"] for o in orders)

    return {
        "total_users":   len(users),
        "plan_breakdown": plan_count,
        "total_orders":  len(orders),
        "total_revenue": total_revenue,
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
