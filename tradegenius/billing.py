"""
Access control and subscriptions.

BILLING_ENABLED=false (default while testing): everyone allowed (subject to
ALLOWED_USER_IDS if set). BILLING_ENABLED=true: an active subscription is
required. Admins (ADMIN_IDS) are always free.
"""

from datetime import datetime, timedelta, timezone

from .config import settings
from .db import parse_iso


def access(user, cfg=settings):
    """Return (allowed: bool, reason: str). reason in ok|admin|blocked|not_allowed|no_subscription."""
    uid = user["id"]
    if cfg.is_admin(uid):
        return True, "admin"
    if user.get("is_blocked"):
        return False, "blocked"
    if cfg.allowed_user_ids and uid not in cfg.allowed_user_ids:
        return False, "not_allowed"
    if not cfg.billing_enabled:
        return True, "ok"
    exp = parse_iso(user.get("sub_expires_at"))
    if exp and exp > datetime.now(timezone.utc):
        return True, "ok"
    return False, "no_subscription"


def extend_subscription(db, uid, days):
    user = db.get_user(uid)
    now = datetime.now(timezone.utc)
    exp = parse_iso(user.get("sub_expires_at")) if user else None
    base = exp if exp and exp > now else now
    new = (base + timedelta(days=days)).replace(microsecond=0)
    db.set_sub_expiry(uid, new.isoformat())
    return new


def maybe_start_trial(db, user, cfg=settings):
    if cfg.billing_enabled and cfg.trial_days > 0 and not user.get("sub_expires_at"):
        return extend_subscription(db, user["id"], cfg.trial_days)
    return None


def subscription_text(user, cfg=settings):
    allowed, reason = access(user, cfg)
    if reason == "admin":
        return "👑 Admin — cheksiz bepul foydalanish"
    if not cfg.billing_enabled:
        return "🧪 Sinov rejimi — hozircha barcha funksiyalar bepul"
    exp = parse_iso(user.get("sub_expires_at"))
    if allowed and exp:
        return f"✅ Obuna faol: {exp.strftime('%Y-%m-%d')} gacha"
    return "❌ Obuna faol emas"
