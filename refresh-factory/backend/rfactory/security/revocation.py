"""Token revocation for bearer tokens that are otherwise trusted until they expire.

Two ways to revoke, both checked on every request, both kept across restarts:
  * a single token, by its `jti` claim (kept until the token would have expired anyway);
  * everything a subject holds, by a cutoff time: a token whose `iat` is at or before the cutoff is refused. A token with no `iat`
    cannot be shown to be newer than the cutoff, so it is refused too.

Limits, stated plainly: it only knows what is told to it (there is no introspection call to the identity provider, and revoking here does not
end the person's session at the identity provider: they can sign in again and get a newer token, so disable the account there too); a token
without a `jti` cannot be revoked individually (set RFACTORY_OIDC_REQUIRE_JTI=1 to refuse such tokens); the cutoff uses this platform's
clock against the identity provider's `iat`, so a skewed clock can leave a just-issued token alive or kill a just-issued new one.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .oidc import AuthError


@dataclass
class RevocationList:
    jtis: dict[str, dict] = field(default_factory=dict)  # jti -> {exp, by, at, reason}
    subjects: dict[str, dict] = field(default_factory=dict)  # sub -> {cutoff, by, at, reason}

    def check(self, claims: dict, now: float | None = None) -> None:
        jti = claims.get("jti")
        if jti and jti in self.jtis:
            raise AuthError("revoked", "this token was revoked")
        s = self.subjects.get(str(claims.get("sub", "")))
        if s is not None:
            iat = claims.get("iat")
            if not isinstance(iat, (int, float)) or iat <= s["cutoff"]:
                raise AuthError("revoked", "this subject's tokens were revoked")

    def revoke_token(self, jti: str, exp: float, by: str, reason: str = "", now: float | None = None) -> None:
        now = time.time() if now is None else now
        self.jtis[jti] = {"exp": float(exp), "by": by, "at": now, "reason": reason[:200]}
        self.prune(now)

    def revoke_subject(self, sub: str, by: str, reason: str = "", now: float | None = None) -> float:
        now = time.time() if now is None else now
        self.subjects[sub] = {"cutoff": now, "by": by, "at": now, "reason": reason[:200]}
        return now

    def prune(self, now: float | None = None) -> None:
        """A revoked token that has expired anyway no longer needs its entry (subject cutoffs are kept: they are small and never expire)."""
        now = time.time() if now is None else now
        self.jtis = {j: v for j, v in self.jtis.items() if v["exp"] > now - 60}

    def public(self) -> dict:
        return {"tokens": [{"jti": j[:8] + "…", **{k: v for k, v in e.items()}} for j, e in self.jtis.items()],
                "subjects": [{"subject": s, **e} for s, e in self.subjects.items()]}
