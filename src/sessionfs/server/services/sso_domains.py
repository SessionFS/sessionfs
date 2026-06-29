"""Shared SSO domain constants — centralized so P2 + P3 import the same denylist.

SSO-P3 (tk_0de967a55afe4896): extracted from routes/auth_sso.py so the
domain-verification admin surface can enforce the same free-email-provider
denylist without duplicating the frozenset.
"""

from __future__ import annotations

# Email-provider denylist — domains an org CANNOT verify (design §2.2).
# An org must not be able to claim gmail.com and JIT-provision every
# Gmail user into its tenant. This is a hard gate, not advisory.
FREE_EMAIL_DENYLIST: frozenset[str] = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com",
    "yahoo.com", "ymail.com", "icloud.com", "me.com", "mac.com",
    "protonmail.com", "proton.me", "mail.com", "aol.com",
    "live.com", "msn.com", "zoho.com", "fastmail.com",
})
