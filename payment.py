"""
Payment endpoints — Momo QR + Bank Transfer
"""
import os, uuid, hmac, hashlib, json
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Depends, Form
from pydantic import BaseModel

router = APIRouter(prefix="/api/payment", tags=["payment"])

SUPABASE_URL     = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE = os.environ.get("SUPABASE_SERVICE_KEY", "")

# Momo config (lấy từ Momo Business sau khi đăng ký)
MOMO_PARTNER_CODE = os.environ.get("MOMO_PARTNER_CODE", "MOMO_TEST")
MOMO_ACCESS_KEY   = os.environ.get("MOMO_ACCESS_KEY", "")
MOMO_SECRET_KEY   = os.environ.get("MOMO_SECRET_KEY", "")
MOMO_ENDPOINT     = "https://test-payment.momo.vn/v2/gateway/api/create"  # test env

# Bank info
BANK_INFO = {
    "bank_name":    os.environ.get("BANK_NAME",    "Vietcombank"),
    "account_number": os.environ.get("BANK_ACCOUNT", "1234567890"),
    "account_name": os.environ.get("BANK_OWNER",   "NGUYEN VAN A"),
    "branch":       os.environ.get("BANK_BRANCH",  "Chi nhánh Hà Nội"),
}

PLAN_PRICES = {
    "basic": {"amount": 120000, "label": "Basic — 100 lần AI/tháng"},
    "pro":   {"amount": 360000, "label": "Pro — Không giới hạn/tháng"},
}


# ── Supabase helpers ───────────────────────────────────────────────────────────

async def sb_get(path: str):
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{SUPABASE_URL}{path}",
            headers={"Authorization": f"Bearer {SUPABASE_SERVICE}", "apikey": SUPABASE_SERVICE},
            timeout=5)
    return r.json()

async def sb_post(path: str, body: dict, method="POST"):
    async with httpx.AsyncClient() as c:
        fn = c.post if method == "POST" else c.patch
        r = await fn(f"{SUPABASE_URL}{path}",
            headers={"Authorization": f"Bearer {SUPABASE_SERVICE}", "apikey": SUPABASE_SERVICE,
                     "Content-Type": "application/json", "Prefer": "return=minimal"},
            json=body, timeout=5)
    return r


# ── Tạo đơn thanh toán ────────────────────────────────────────────────────────

@router.post("/create-order")
async def create_order(
    plan:   str = Form(...),
    method: str = Form(...),   # momo | bank
    user_id: str = Form(...),
    user_email: str = Form(...),
):
    if plan not in PLAN_PRICES:
        raise HTTPException(400, "Plan không hợp lệ")
    if method not in ("momo", "bank"):
        raise HTTPException(400, "Phương thức không hợp lệ")

    price_info = PLAN_PRICES[plan]
    order_id   = f"FC-{uuid.uuid4().hex[:10].upper()}"
    amount     = price_info["amount"]

    # Lưu đơn vào Supabase
    await sb_post("/rest/v1/payment_orders", {
        "id":         order_id,
        "user_id":    user_id,
        "user_email": user_email,
        "plan":       plan,
        "amount":     amount,
        "method":     method,
        "status":     "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })

    if method == "momo":
        return await _create_momo_order(order_id, amount, plan, user_email)
    else:
        return _create_bank_order(order_id, amount, plan, user_email)


def _create_bank_order(order_id, amount, plan, email):
    """Trả về thông tin chuyển khoản."""
    content = f"FC {order_id}"  # nội dung CK để admin nhận biết
    vietqr_url = (
        f"https://img.vietqr.io/image/{BANK_INFO['bank_name']}-"
        f"{BANK_INFO['account_number']}-compact2.png"
        f"?amount={amount}&addInfo={content}&accountName={BANK_INFO['account_name']}"
    )
    return {
        "method":      "bank",
        "order_id":    order_id,
        "amount":      amount,
        "bank_info":   BANK_INFO,
        "content":     content,
        "qr_url":      vietqr_url,
        "plan_label":  PLAN_PRICES[plan]["label"],
        "note":        "Sau khi chuyển khoản, admin sẽ xác nhận trong vòng 24 giờ.",
    }


async def _create_momo_order(order_id, amount, plan, email):
    """Tạo QR Momo."""
    if not MOMO_ACCESS_KEY:
        # Chưa có key → trả về hướng dẫn
        return {
            "method":   "momo_pending",
            "order_id": order_id,
            "note":     "Momo chưa được cấu hình. Vui lòng dùng chuyển khoản ngân hàng.",
        }

    request_id = uuid.uuid4().hex
    raw_sig = (
        f"accessKey={MOMO_ACCESS_KEY}&amount={amount}&extraData="
        f"&ipnUrl={os.environ.get('BASE_URL','')}/api/payment/momo-ipn"
        f"&orderId={order_id}&orderInfo={PLAN_PRICES[plan]['label']}"
        f"&partnerCode={MOMO_PARTNER_CODE}&redirectUrl="
        f"{os.environ.get('BASE_URL','')}/payment-result"
        f"&requestId={request_id}&requestType=paymentCode"
    )
    sig = hmac.new(MOMO_SECRET_KEY.encode(), raw_sig.encode(), hashlib.sha256).hexdigest()

    payload = {
        "partnerCode": MOMO_PARTNER_CODE,
        "requestId":   request_id,
        "amount":      amount,
        "orderId":     order_id,
        "orderInfo":   PLAN_PRICES[plan]["label"],
        "redirectUrl": f"{os.environ.get('BASE_URL','')}/payment-result",
        "ipnUrl":      f"{os.environ.get('BASE_URL','')}/api/payment/momo-ipn",
        "requestType": "paymentCode",
        "extraData":   "",
        "lang":        "vi",
        "signature":   sig,
    }

    async with httpx.AsyncClient() as c:
        r = await c.post(MOMO_ENDPOINT, json=payload, timeout=10)
    data = r.json()

    if data.get("resultCode") != 0:
        raise HTTPException(500, f"Momo lỗi: {data.get('message')}")

    return {
        "method":    "momo",
        "order_id":  order_id,
        "amount":    amount,
        "qr_url":    data.get("qrCodeUrl"),
        "deep_link": data.get("deeplink"),
        "pay_url":   data.get("payUrl"),
    }


# ── Momo IPN callback ─────────────────────────────────────────────────────────

@router.post("/momo-ipn")
async def momo_ipn(request_data: dict):
    """Momo gọi về đây sau khi user thanh toán."""
    order_id    = request_data.get("orderId")
    result_code = request_data.get("resultCode")

    if result_code == 0:
        await _confirm_order(order_id)

    return {"status": "ok"}


# ── Admin: xác nhận đơn (chuyển khoản thủ công) ───────────────────────────────

@router.post("/confirm-order")
async def confirm_order(
    order_id:     str = Form(...),
    admin_secret: str = Form(...),
):
    """Admin xác nhận đơn hàng đã thanh toán."""
    if admin_secret != os.environ.get("ADMIN_SECRET", "change-me-secret"):
        raise HTTPException(403, "Không có quyền")

    result = await _confirm_order(order_id)
    return result


async def _confirm_order(order_id: str):
    """Nâng plan user sau khi thanh toán xác nhận."""
    # Lấy đơn hàng
    orders = await sb_get(f"/rest/v1/payment_orders?id=eq.{order_id}&select=user_id,plan,status")
    if not orders:
        raise HTTPException(404, "Không tìm thấy đơn hàng")

    order = orders[0]
    if order["status"] == "completed":
        return {"message": "Đơn đã được xử lý trước đó"}

    # Nâng plan
    await sb_post(
        f"/rest/v1/profiles?id=eq.{order['user_id']}",
        {"plan": order["plan"], "updated_at": datetime.now(timezone.utc).isoformat()},
        method="PATCH",
    )

    # Cập nhật trạng thái đơn
    await sb_post(
        f"/rest/v1/payment_orders?id=eq.{order_id}",
        {"status": "completed", "confirmed_at": datetime.now(timezone.utc).isoformat()},
        method="PATCH",
    )

    return {"success": True, "message": f"Đã nâng plan {order['plan']} cho user {order['user_id']}"}


# ── Lấy lịch sử đơn hàng ─────────────────────────────────────────────────────

@router.get("/orders/{user_id}")
async def get_orders(user_id: str):
    orders = await sb_get(
        f"/rest/v1/payment_orders?user_id=eq.{user_id}&order=created_at.desc&limit=10"
    )
    return orders
