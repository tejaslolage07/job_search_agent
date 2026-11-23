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
:root{--bg:#0d0d10;--card:#16161b;--card2:#1b1b22;--ink:#f3f3f6;--muted:#9a9aa8;--line:#25252d;--accent:#8b5cf6;--accent2:#a78bfa;--glow:rgba(139,92,246,.35);--bad:#f87171;--g1:#a78bfa;--g2:#c4b5fd;--g3:#f0abfc;color-scheme:dark}
@media (prefers-color-scheme:light){:root{--bg:#f4f4f8;--card:#fff;--card2:#f6f5fb;--ink:#17171c;--muted:#6b6b7b;--line:#e6e6ee;--accent:#7c3aed;--accent2:#6d28d9;--glow:rgba(124,58,237,.25);--bad:#dc2626;--g1:#6d28d9;--g2:#7c3aed;--g3:#c026d3;color-scheme:light}}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;background:var(--bg);color:var(--ink);font:15px/1.55 Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;-webkit-font-smoothing:antialiased}
.wrap{min-height:100vh;display:grid;grid-template-columns:1.1fr .9fr}
.hero{position:relative;overflow:hidden;padding:56px clamp(24px,5vw,72px);display:flex;flex-direction:column;justify-content:center;gap:28px;background:radial-gradient(900px 500px at 15% 0%,rgba(139,92,246,.45),transparent 60%),radial-gradient(700px 500px at 90% 100%,rgba(99,102,241,.3),transparent 60%),var(--bg);border-right:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:12px;font-weight:700;font-size:1.05rem}
.logo{width:38px;height:38px;border-radius:12px;background:linear-gradient(135deg,var(--accent2),var(--accent));display:grid;place-items:center;box-shadow:0 6px 20px var(--glow)}
.logo svg{width:20px;height:20px;stroke:#fff;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
h1{font-size:clamp(2rem,3.6vw,3rem);line-height:1.1;letter-spacing:-.02em;margin:0;max-width:560px}
h1 span{background:linear-gradient(135deg,var(--g1),var(--g2) 60%,var(--g3));-webkit-background-clip:text;background-clip:text;color:transparent}
.lead{color:var(--muted);font-size:1.05rem;max-width:520px;margin:0}
.feats{display:grid;gap:14px;max-width:540px}
.feat{display:flex;gap:14px;align-items:flex-start;padding:14px 16px;border-radius:16px;background:color-mix(in srgb,var(--card) 70%,transparent);border:1px solid var(--line);backdrop-filter:blur(8px)}
.feat i{flex:none;width:36px;height:36px;border-radius:10px;display:grid;place-items:center;background:color-mix(in srgb,var(--accent) 20%,transparent);color:var(--accent2)}
.feat svg{width:18px;height:18px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.feat b{display:block;font-size:.95rem;margin-bottom:2px}.feat p{margin:0;color:var(--muted);font-size:.88rem}
.side{display:flex;align-items:center;justify-content:center;padding:40px 20px}
.panel{width:100%;max-width:410px;background:var(--card);border:1px solid var(--line);border-radius:22px;padding:30px;box-shadow:0 20px 60px rgba(0,0,0,.35)}
.panel h2{margin:0 0 4px;font-size:1.45rem;letter-spacing:-.01em}.sub{margin:0 0 20px;color:var(--muted);font-size:.9rem}
.tabs{display:grid;grid-template-columns:1fr 1fr;gap:4px;padding:4px;margin-bottom:22px;border-radius:12px;background:var(--card2);border:1px solid var(--line)}
.tabs label{margin:0;text-align:center;padding:9px;border-radius:9px;cursor:pointer;font-weight:600;font-size:.9rem;color:var(--muted);transition:.15s}
input.tab{position:absolute;opacity:0;pointer-events:none}
.form{display:none}
#t-in:checked~.tabs label[for=t-in],#t-up:checked~.tabs label[for=t-up]{background:linear-gradient(135deg,var(--accent2),var(--accent));color:#fff;box-shadow:0 4px 14px var(--glow)}
#t-in:checked~#f-in,#t-up:checked~#f-up{display:block}
#t-in:focus-visible~.tabs label[for=t-in],#t-up:focus-visible~.tabs label[for=t-up]{outline:2px solid var(--accent2);outline-offset:2px}
.f{margin-bottom:14px}.f label{display:block;font-size:.8rem;font-weight:500;color:var(--muted);margin-bottom:6px}
input:not(.tab){width:100%;padding:11px 13px;border:1px solid var(--line);border-radius:11px;background:var(--card2);color:var(--ink);font:inherit;outline:none;transition:.15s}
input:not(.tab):focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--glow)}
button{margin-top:6px;width:100%;padding:12px;border:0;border-radius:11px;background:linear-gradient(135deg,var(--accent2),var(--accent));color:#fff;font:inherit;font-weight:600;cursor:pointer;box-shadow:0 6px 18px var(--glow);transition:.15s}
button:hover{transform:translateY(-1px)}
.msg{display:flex;gap:8px;align-items:center;background:color-mix(in srgb,var(--bad) 12%,transparent);border:1px solid color-mix(in srgb,var(--bad) 50%,transparent);color:var(--bad);border-radius:11px;padding:10px 12px;margin-bottom:16px;font-size:.88rem}
.note{margin:18px 0 0;color:var(--muted);font-size:.8rem;text-align:center}
@media (max-width:860px){.wrap{grid-template-columns:1fr}.hero{border-right:0;border-bottom:1px solid var(--line);padding:32px 20px;gap:20px}.feats{display:none}.side{padding:24px 16px 40px}}
</style></head><body><div class="wrap">
<section class="hero">
<div class="brand"><span class="logo"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg></span>Job Search Agent</div>
<h1>Find new jobs <span>the day they are posted.</span></h1>
<p class="lead">It watches the career pages of the companies you pick, spots new listings, and ranks them against your profile and resume, so you read the few worth your time instead of hundreds.</p>
<div class="feats">
<div class="feat"><i><svg viewBox="0 0 24 24"><path d="M3 21h18M5 21V7l7-4 7 4v14M9 9h1m4 0h1M9 13h1m4 0h1M9 17h1m4 0h1"/></svg></i><div><b>Watches career pages</b><p>Pick companies from a catalog or add your own. New and changed listings are detected on a schedule you set.</p></div></div>
<div class="feat"><i><svg viewBox="0 0 24 24"><path d="M12 3l2.1 5.4L20 9l-4.4 3.8L17 18.5 12 15.6 7 18.5l1.4-5.7L4 9l5.9-.6z"/></svg></i><div><b>Matches with AI</b><p>Each job is judged against your profile first, then checked against your resume. Newest first.</p></div></div>
<div class="feat"><i><svg viewBox="0 0 24 24"><path d="M12 3l8 3v6c0 4.5-3.2 7.9-8 9-4.8-1.1-8-4.5-8-9V6z"/><path d="m9 12 2 2 4-4"/></svg></i><div><b>Never applies for you</b><p>It only discovers and ranks. You open the posting and apply yourself. Your data stays in your own private workspace.</p></div></div>
</div></section>
<section class="side"><div class="panel">
__MSG__
<input class="tab" type="radio" name="tab" id="t-in"__IN__><input class="tab" type="radio" name="tab" id="t-up"__UP__>
<div class="tabs"><label for="t-in">Sign in</label><label for="t-up">Sign up</label></div>
<form class="form" id="f-in" method="post" action="/login"><h2>Welcome back</h2><p class="sub">Sign in to see today's matches.</p>
<div class="f"><label for="le">Email</label><input id="le" name="email" type="email" autocomplete="email" placeholder="you@example.com" required maxlength="200"></div>
<div class="f"><label for="lp">Password</label><input id="lp" name="password" type="password" autocomplete="current-password" placeholder="Your password" required maxlength="200"></div>
<button type="submit">Sign in</button></form>
<form class="form" id="f-up" method="post" action="/signup"><h2>Create an account</h2><p class="sub">Set up your private workspace in a minute.</p>
<div class="f"><label for="se">Email</label><input id="se" name="email" type="email" autocomplete="email" placeholder="you@example.com" required maxlength="200"></div>
<div class="f"><label for="sp">Password (8+ characters)</label><input id="sp" name="password" type="password" autocomplete="new-password" placeholder="Choose a password" required minlength="8" maxlength="200"></div>
<div class="f"><label for="sc">Invitation code</label><input id="sc" name="master_code" type="password" autocomplete="off" placeholder="From whoever shared this link" required maxlength="200"></div>
<button type="submit">Create account</button></form>
<p class="note">Access is by invitation. Ask the person who shared this link for the code.</p>
</div></section></div></body></html>"""


def render(error: str | None = None) -> str:
    msg = MESSAGES.get(error or "")
    signup = error in ("code", "email", "pw", "exists")  # these come from the sign-up form, so open that tab
    page = _PAGE.replace("__MSG__", f'<div class="msg" role="alert">{msg}</div>' if msg else "")
    return page.replace("__IN__", "" if signup else " checked").replace("__UP__", " checked" if signup else "")
