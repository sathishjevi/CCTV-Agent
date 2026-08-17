"""Security response headers — shared between both Floorwatch services
(DATA_PROTECTION_SECURITY_ANALYSIS.md DP-M2: "No security headers on
either service"). One shared module rather than copied into each
service, same reasoning as floorwatch_rate_limit.py's module docstring.

Honest limitation, not glossed over: the Content-Security-Policy below
allows 'unsafe-inline' for script-src and style-src. Both dashboards
(dashboard/floorwatch_demo.html, dashboard/floorwatch_chat.html) are
single self-contained HTML files with all their JS/CSS inline and no
nonce/hash infrastructure — a strict script-src without 'unsafe-inline'
would break every button and API call on both pages. This means the CSP
does NOT block an injected inline <script> tag from executing (that's
what the actual output-escaping fixes are for — see escapeHtml() in
floorwatch_demo.html, DP-H1/DP-H2). What it DOES still meaningfully add:
blocking any REMOTE script/style/image load an attacker-injected payload
might attempt (script-src/style-src/img-src all 'self' otherwise),
blocking clickjacking via framing (frame-ancestors 'none'), and blocking
exfiltration via a hijacked <form> action to another origin
(form-action 'self'). Real defense-in-depth, not a complete substitute
for fixing injection at the source — both matter, neither alone is enough.

Both dashboards are confirmed to load zero external resources (no CDN
scripts, no remote fonts/images) — verified before writing this policy,
so default-src 'self' doesn't break anything that currently works.
"""


def install_security_headers(app):
    """Adds a middleware to `app` (a FastAPI instance) that sets standard
    defense-in-depth headers on every response. Call once, after the app
    is constructed — e.g. `install_security_headers(app)` right after
    `app = FastAPI(...)`."""

    @app.middleware("http")
    async def _add_security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        # Only meaningful over HTTPS (the public-facing connection — e.g.
        # Railway terminates TLS at the edge even though this process
        # itself speaks plain HTTP internally); browsers apply it based on
        # the origin's scheme as the client actually saw it, so this is
        # safe to always set regardless of what this process sees locally.
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "object-src 'none'"
        )
        return response
