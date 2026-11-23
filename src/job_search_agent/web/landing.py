"""Public landing page: what the tool does, plus the sign-in and sign-up forms. Messages are fixed strings (nothing user-supplied is echoed)."""
from __future__ import annotations

MESSAGES = {
    "code": "That invitation code is not valid.",
    "email": "Please enter a valid email address.",
    "pw": "Choose a password of 8 to 200 characters.",
    "exists": "An account with that email already exists. Sign in instead.",
    "login": "Wrong email or password.",
    "slow": "Too many attempts. Please wait 15 minutes and try again.",
}

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Job Search Agent</title>
<style>
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--muted:#63635e;--line:#e2e2dc;--accent:#2f5d50;--accent-ink:#fff;--bad:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#161715;--card:#1f201e;--ink:#ecece8;--muted:#a3a39c;--line:#33342f;--accent:#7fb8a4;--accent-ink:#10201a;--bad:#f2928b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:980px;margin:0 auto;padding:48px 16px 64px}
h1{font-size:2.1rem;line-height:1.15;margin:0 0 12px}h2{font-size:1.05rem;margin:0 0 6px}
.lead{font-size:1.15rem;color:var(--muted);max-width:640px;margin:0 0 36px}
.grid{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));margin-bottom:40px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px}
.card p{margin:0;color:var(--muted);font-size:.95rem}
.forms{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
label{display:block;font-size:.85rem;color:var(--muted);margin:12px 0 4px}
input{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);font:inherit}
button{margin-top:16px;width:100%;padding:11px;border:0;border-radius:8px;background:var(--accent);color:var(--accent-ink);font:inherit;font-weight:600;cursor:pointer}
.msg{background:var(--card);border:1px solid var(--bad);color:var(--bad);border-radius:8px;padding:10px 12px;margin-bottom:16px}
.note{color:var(--muted);font-size:.85rem;margin-top:28px}
</style></head><body><main>
<h1>Find new jobs the day they are posted.</h1>
<p class="lead">Job Search Agent watches the career pages of the companies you pick, spots listings that are new, and ranks them against your profile and resume, so you read the few worth your time instead of hundreds.</p>
<div class="grid">
<div class="card"><h2>Watches career pages</h2><p>Pick companies from a catalog or add your own. New and changed listings are detected on a schedule you set.</p></div>
<div class="card"><h2>Matches with AI</h2><p>Each job is judged against your profile first, then checked against your resume. You see matched, to review and not a fit, newest first.</p></div>
<div class="card"><h2>Never applies for you</h2><p>It only discovers and ranks. You open the posting and apply yourself. Your data stays in your own private workspace.</p></div>
</div>
__MSG__
<div class="forms">
<form class="card" method="post" action="/signup"><h2>Create an account</h2>
<label for="se">Email</label><input id="se" name="email" type="email" autocomplete="email" required maxlength="200">
<label for="sp">Password (8+ characters)</label><input id="sp" name="password" type="password" autocomplete="new-password" required minlength="8" maxlength="200">
<label for="sc">Invitation code</label><input id="sc" name="master_code" type="password" autocomplete="off" required maxlength="200">
<button type="submit">Create account</button></form>
<form class="card" method="post" action="/login"><h2>Sign in</h2>
<label for="le">Email</label><input id="le" name="email" type="email" autocomplete="email" required maxlength="200">
<label for="lp">Password</label><input id="lp" name="password" type="password" autocomplete="current-password" required maxlength="200">
<button type="submit">Sign in</button></form>
</div>
<p class="note">Access is by invitation. Ask the person who shared this link for the invitation code.</p>
</main></body></html>"""


def render(error: str | None = None) -> str:
    msg = MESSAGES.get(error or "")
    return _PAGE.replace("__MSG__", f'<div class="msg" role="alert">{msg}</div>' if msg else "")
