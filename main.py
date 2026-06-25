"""
SONEX Phase 2 Backend — FastAPI + Stripe + Supabase
Compatible with Pydantic v1, FastAPI 0.100, Supabase-py 1.2
Deploy to Render as a Web Service (Python 3.11)
"""

import os
import stripe
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from supabase import create_client, Client

# ============================================================
# INIT
# ============================================================
app = FastAPI(title="SONEX Backend", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://sonex-seven.vercel.app",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://sonex-seven.vercel.app")

# ============================================================
# PLANS CONFIG
# ============================================================
PLANS = {
    "premium_monthly": {
        "name": "Listener Premium",
        "price_usd": 7.99,
        "interval": "month",
        "profile_field": "listener_plan",
        "profile_value": "premium",
    },
    "premium_annual": {
        "name": "Listener Premium (Annual)",
        "price_usd": 79.99,
        "interval": "year",
        "profile_field": "listener_plan",
        "profile_value": "premium",
    },
    "artist_pro_monthly": {
        "name": "Artist Pro",
        "price_usd": 14.99,
        "interval": "month",
        "profile_field": "artist_plan",
        "profile_value": "pro",
    },
    "artist_pro_annual": {
        "name": "Artist Pro (Annual)",
        "price_usd": 149.99,
        "interval": "year",
        "profile_field": "artist_plan",
        "profile_value": "pro",
    },
}

# ============================================================
# MODELS (Pydantic v1 style)
# ============================================================
class CheckoutRequest(BaseModel):
    plan: str
    user_id: str
    user_email: str
    auto_renew: Optional[bool] = True

class WalletTransferRequest(BaseModel):
    artist_id: str
    month: str

class WithdrawRequest(BaseModel):
    artist_id: str
    amount_usd: float
    method: str
    destination: str

class AutoRenewRequest(BaseModel):
    user_id: str
    stripe_subscription_id: str
    enabled: bool

# ============================================================
# HEALTH CHECK
# ============================================================
@app.get("/")
def health():
    return {"status": "SONEX backend is running", "version": "2.0.0"}

# ============================================================
# STRIPE CHECKOUT
# ============================================================
@app.post("/create-checkout-session")
async def create_checkout_session(req: CheckoutRequest):
    plan = PLANS.get(req.plan)
    if not plan:
        raise HTTPException(status_code=400, detail=f"Unknown plan: {req.plan}")

    try:
        price = stripe.Price.create(
            unit_amount=int(plan["price_usd"] * 100),
            currency="usd",
            recurring={"interval": plan["interval"]},
            product_data={"name": plan["name"]},
        )

        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price": price.id, "quantity": 1}],
            mode="subscription",
            success_url=f"{FRONTEND_URL}?checkout=success&plan={req.plan}&user_id={req.user_id}",
            cancel_url=f"{FRONTEND_URL}?checkout=cancelled",
            customer_email=req.user_email,
            metadata={
                "user_id": req.user_id,
                "plan": req.plan,
                "auto_renew": str(req.auto_renew).lower(),
            },
            subscription_data={
                "metadata": {
                    "user_id": req.user_id,
                    "plan": req.plan,
                }
            },
        )

        return {"checkout_url": session.url, "session_id": session.id}

    except stripe.error.StripeError as e:
        raise HTTPException(status_code=400, detail=str(e))

# ============================================================
# STRIPE WEBHOOK
# ============================================================
@app.post("/webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(None, alias="stripe-signature"),
):
    payload = await request.body()

    try:
        event = stripe.Webhook.construct_event(
            payload, stripe_signature, STRIPE_WEBHOOK_SECRET
        )
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    event_type = event["type"]

    if event_type == "checkout.session.completed":
        await handle_successful_payment(event["data"]["object"])
    elif event_type == "customer.subscription.deleted":
        await handle_subscription_cancelled(event["data"]["object"])
    elif event_type == "invoice.payment_failed":
        await handle_payment_failed(event["data"]["object"])

    return {"received": True}

async def handle_successful_payment(session):
    metadata = session.get("metadata", {})
    user_id = metadata.get("user_id")
    plan_key = metadata.get("plan")
    auto_renew = metadata.get("auto_renew", "true") == "true"
    if not user_id or not plan_key:
        return
    plan = PLANS.get(plan_key)
    if not plan:
        return

    supabase.table("profiles").update({
        plan["profile_field"]: plan["profile_value"]
    }).eq("id", user_id).execute()

    supabase.table("subscriptions").upsert({
        "user_id": user_id,
        "plan": plan_key,
        "status": "active",
        "auto_renew": auto_renew,
        "stripe_subscription_id": session.get("subscription"),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }).execute()

    print(f"[SONEX] Plan activated: {plan_key} for user {user_id}")

async def handle_subscription_cancelled(sub):
    user_id = sub.get("metadata", {}).get("user_id")
    plan_key = sub.get("metadata", {}).get("plan")
    if not user_id or not plan_key:
        return
    plan = PLANS.get(plan_key)
    if plan:
        reset_value = "free" if plan["profile_field"] == "listener_plan" else "basic"
        supabase.table("profiles").update({
            plan["profile_field"]: reset_value
        }).eq("id", user_id).execute()
    supabase.table("subscriptions").update({
        "status": "cancelled"
    }).eq("stripe_subscription_id", sub["id"]).execute()

async def handle_payment_failed(invoice):
    subscription_id = invoice.get("subscription")
    if subscription_id:
        supabase.table("subscriptions").update({
            "status": "past_due"
        }).eq("stripe_subscription_id", subscription_id).execute()

# ============================================================
# AUTO-RENEW TOGGLE
# ============================================================
@app.post("/toggle-auto-renew")
async def toggle_auto_renew(req: AutoRenewRequest):
    try:
        stripe.Subscription.modify(
            req.stripe_subscription_id,
            cancel_at_period_end=not req.enabled,
        )
        supabase.table("subscriptions").update({
            "auto_renew": req.enabled
        }).eq("stripe_subscription_id", req.stripe_subscription_id).execute()
        return {"success": True, "auto_renew": req.enabled}
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=400, detail=str(e))

# ============================================================
# WALLET SYSTEM
# ============================================================
@app.get("/wallet/{artist_id}")
async def get_wallet(artist_id: str):
    result = supabase.table("wallet").select("*").eq("artist_id", artist_id).execute()
    if result.data:
        return result.data[0]
    return {"artist_id": artist_id, "balance_usd": 0.0, "last_transfer_at": None}

@app.post("/wallet/transfer-monthly-earnings")
async def transfer_monthly_earnings(req: WalletTransferRequest):
    result = supabase.table("royalties").select("*") \
        .eq("artist_id", req.artist_id) \
        .eq("month", req.month) \
        .eq("status", "pending") \
        .execute()

    royalties = result.data or []
    if not royalties:
        return {"transferred": 0, "message": "No pending royalties for this month"}

    total = sum(float(r["amount_usd"]) for r in royalties)

    wallet_result = supabase.table("wallet").select("*") \
        .eq("artist_id", req.artist_id).execute()

    if wallet_result.data:
        current_balance = float(wallet_result.data[0]["balance_usd"] or 0)
        supabase.table("wallet").update({
            "balance_usd": current_balance + total,
            "last_transfer_at": datetime.now(timezone.utc).isoformat(),
        }).eq("artist_id", req.artist_id).execute()
    else:
        supabase.table("wallet").insert({
            "artist_id": req.artist_id,
            "balance_usd": total,
            "last_transfer_at": datetime.now(timezone.utc).isoformat(),
        }).execute()

    royalty_ids = [r["id"] for r in royalties]
    for rid in royalty_ids:
        supabase.table("royalties").update({"status": "transferred"}) \
            .eq("id", rid).execute()

    return {
        "transferred": total,
        "month": req.month,
        "message": f"${total:.2f} moved to wallet"
    }

@app.post("/wallet/withdraw")
async def request_withdrawal(req: WithdrawRequest):
    wallet = supabase.table("wallet").select("*") \
        .eq("artist_id", req.artist_id).execute()

    if not wallet.data:
        raise HTTPException(status_code=404, detail="Wallet not found")

    balance = float(wallet.data[0]["balance_usd"] or 0)
    if req.amount_usd > balance:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient balance: ${balance:.2f} available"
        )
    if req.amount_usd < 5.00:
        raise HTTPException(
            status_code=400,
            detail="Minimum withdrawal is $5.00"
        )

    supabase.table("wallet").update({
        "balance_usd": balance - req.amount_usd
    }).eq("artist_id", req.artist_id).execute()

    supabase.table("withdrawals").insert({
        "artist_id": req.artist_id,
        "amount_usd": req.amount_usd,
        "method": req.method,
        "destination": req.destination,
        "status": "pending",
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }).execute()

    return {
        "success": True,
        "withdrawn": req.amount_usd,
        "remaining": round(balance - req.amount_usd, 2)
    }

# ============================================================
# ROYALTY CALCULATION
# ============================================================
@app.post("/calculate-royalties/{month}")
async def calculate_royalties(month: str):
    streams_result = supabase.table("streams").select("track_id").execute()
    streams = streams_result.data or []

    track_ids = list(set(s["track_id"] for s in streams if s.get("track_id")))
    if not track_ids:
        return {"month": month, "artists_calculated": 0, "total_payout_usd": 0}

    tracks_result = supabase.table("tracks").select(
        "id, artist_id, ai_rate, content_type"
    ).in_("id", track_ids).execute()
    tracks_map = {t["id"]: t for t in (tracks_result.data or [])}

    artist_ids = list(set(t["artist_id"] for t in tracks_map.values() if t.get("artist_id")))
    profiles_result = supabase.table("profiles").select(
        "id, artist_plan"
    ).in_("id", artist_ids).execute()
    profiles_map = {p["id"]: p for p in (profiles_result.data or [])}

    earnings_by_artist = {}
    for stream in streams:
        track = tracks_map.get(stream.get("track_id"))
        if not track:
            continue
        artist_id = track.get("artist_id")
        if not artist_id:
            continue
        ai_rate = float(track.get("ai_rate") or 1.0)
        profile = profiles_map.get(artist_id, {})
        artist_plan = profile.get("artist_plan", "basic")

        base_rate = 0.004
        plan_multiplier = 1.0 if artist_plan == "pro" else 0.85
        earning = base_rate * ai_rate * plan_multiplier

        if artist_id not in earnings_by_artist:
            earnings_by_artist[artist_id] = {"streams": 0, "amount": 0.0}
        earnings_by_artist[artist_id]["streams"] += 1
        earnings_by_artist[artist_id]["amount"] += earning

    for artist_id, data in earnings_by_artist.items():
        supabase.table("royalties").upsert({
            "artist_id": artist_id,
            "month": month,
            "stream_count": data["streams"],
            "amount_usd": round(data["amount"], 4),
            "status": "pending",
        }).execute()

    return {
        "month": month,
        "artists_calculated": len(earnings_by_artist),
        "total_payout_usd": round(
            sum(d["amount"] for d in earnings_by_artist.values()), 2
        )
    }
