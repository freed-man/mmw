"""
mmw.py  reg to paint code. One page held open, one reg after another.

    python mmw.py GM14DKE --headed              one reg, prints JSON
    python mmw.py GM14DKE --headed --debug      full diagnostic report
    python mmw.py --file regs.txt               batch, appends to results.csv
    python mmw.py --file regs.txt --expect verified.csv    accuracy run
    python mmw.py --selftest                    pure function checks, no network

The answer is read from the site's own `paint_search_data` cookie, written on
every successful lookup:

    {"vehicle_details":"Volkswagen Golf 2014 1.6 Diesel",
     "colour":"GREY","paint_code":"A7N","reg_no":"gm14dke"}

It carries the reg it belongs to, so it is only accepted when `reg_no` matches
the reg just submitted. Structured DOM read of the result panel is the fallback.

The widget is Knockout. The form's submit handler and the input's value binding
do not exist until KO applies its bindings, and clicking go before that moment
makes the browser do a plain native form submit: the page reloads with the reg
in the query string, KO starts fresh with an empty field, and nothing ever
appears. So every lookup waits for KO to own the input before touching it, and
a navigation after submit is reported rather than waited out.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import csv
import json
import random
import re
import secrets
import string
import time
import urllib.parse
from pathlib import Path
from typing import Any, Optional

URL = "https://www.mymotorworld.com/car-paint-by-reg"
ORIGIN = "mymotorworld.com"
COOKIE_NAME = "paint_search_data"

# The widget's own endpoint, seen in the network trace. Form encoded:
#   data[registration]=GM14DKE&data[form_key]=<form_key>&form_key=<form_key>
# Its response is read directly when it comes back 200, which is faster and
# more certain than waiting for the cookie to be rewritten.
LOOKUP_PATH = "/paintmatching/colour/search"

# The fetch path needs a document on the origin and the form_key cookie, not a
# retail homepage with Maps, New Relic and a chat widget attached. This is the
# site's own section endpoint: same origin, same session, a few hundred bytes.
LIGHT_URL = ("https://www.mymotorworld.com/customer/section/load/"
             "?sections=paint-data")

DEADLINE_S = 25.0
KO_DEADLINE_S = 25.0
POLL_S = 0.12
POPUP_EVERY = 6          # poll iterations between popup probes
RESUBMIT_AFTER_S = 8.0
DELAY_S = (2.0, 4.0)      # overridable with --delay
RELOAD_EVERY = 40

SEL_INPUT = "#paint-vrm-reg"
SEL_GO = "#paint-vrm-search"
SEL_RESET = "#vrm-remove, #paint-vrm-reset"
SEL_RESULT = ".selected-paint-vrm-details"
SEL_HAS_VEHICLE = ".has-paintvehicle"
# :visible on every one of these. Magento keeps several hidden .modal-popup
# elements in the DOM, so .first without it lands on one that is not on screen.
# [role="dialog"] is deliberately absent: the cookie notice uses it too.
SEL_POPUP = ('.modal-popup:visible, .modal-inner-wrap:visible, .swal2-popup:visible, '
             '.message-error:visible, .mage-error:visible')
SEL_POPUP_CLOSE = ('.modal-popup:visible .action-primary',
                   'button[data-role="closeBtn"]:visible',
                   '.modal-popup:visible .action-close',
                   'button:visible:has-text("OK")',
                   '.swal2-confirm:visible')

# The push notification prompt is a vendor overlay that sits over the widget.
# Blocked at the network level below; these are the backstop if it renders.
SEL_OVERLAY_DISMISS = ('button:visible:has-text("Later")',
                       'a:visible:has-text("Later")',
                       '[role="button"]:visible:has-text("Later")',
                       'button:visible:has-text("Not now")',
                       'button:visible:has-text("No thanks")')

# Third party push and chat vendors, seen in the cookie jar and the CSP errors.
# Routing is scoped to these hosts only, so same origin requests are never
# intercepted and cannot pick up the 403 that full interception caused.
PUSH_HOSTS = re.compile(r"(smct\.io|smartech|netcorecloud|smtcdn)")

NOT_FOUND_MARKERS = ("not found", "check entry", "no match", "unable to find",
                     "could not find", "couldn't find", "no vehicle")

# Their two messages mean different things and the difference is the useful
# part: "vehicle X not found" is their vehicle lookup failing, "couldn't find
# paints for X" is the vehicle resolving with no paint data behind it.
UNKNOWN_VEHICLE_MARKERS = ("vehicle", "not found")

BLOCK_FRAGMENTS = (
    "googletagmanager", "google-analytics", "doubleclick", "facebook.net",
    "clarity.ms", "bat.bing.com", "klaviyo", "smartech", "netcorecloud",
    "hotjar", "tiktok", "nr-data.net", "js-agent.newrelic.com", "reviews.co.uk",
)

CF_MARKERS = ("just a moment", "attention required", "cf-challenge",
              "checking your browser", "verify you are human",
              "you have been blocked", "access denied", "enable javascript and cookies")

PLACEHOLDERS = {"ENTER REG", "ENTER YOUR REG", "N/A", "TBC", "UNKNOWN", "-", ""}
CODE_SHAPE = re.compile(r"^[A-Z0-9][A-Z0-9 ./\-]{0,19}$")
REG_CLEAN = re.compile(r"[^A-Z0-9]")


# ---------------------------------------------------------------------------
# pure functions, all covered by --selftest
# ---------------------------------------------------------------------------

def normalise_reg(reg: Optional[str]) -> str:
    return REG_CLEAN.sub("", (reg or "").upper())


def is_valid_code(raw: Optional[str]) -> bool:
    """No digit requirement: digit free codes are real (Dacia OVDQH is dealer
    confirmed). Length and shape carry the check."""
    if not raw:
        return False
    code = str(raw).strip().upper()
    if code in PLACEHOLDERS or not (2 <= len(code) <= 20):
        return False
    if not CODE_SHAPE.match(code):
        return False
    if code.isalpha() and len(code) > 12:
        return False
    return True


def parse_cookie(value: Optional[str]) -> dict[str, str]:
    if not value:
        return {}
    try:
        data = json.loads(urllib.parse.unquote(value))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def cookie_answer(value: Optional[str], reg: str) -> Optional[dict[str, str]]:
    """Only accept the cookie if it is about the reg we just submitted."""
    data = parse_cookie(value)
    if not data:
        return None
    if normalise_reg(data.get("reg_no")) != normalise_reg(reg):
        return None
    code = str(data.get("paint_code", "")).strip().upper()
    if not is_valid_code(code):
        return None
    return {"code": code,
            "colour": (data.get("colour") or "").strip() or None,
            "vehicle": (data.get("vehicle_details") or "").strip() or None}


def search_answer(payload: Any) -> Optional[dict[str, Optional[str]]]:
    """Pull the three fields out of the lookup endpoint's response. The shape is
    not fully known, so this walks nested dicts and lists rather than assuming
    one, and mirrors the key names the cookie uses."""
    found: dict[str, Optional[str]] = {"code": None, "colour": None, "vehicle": None}

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                key = str(k).lower()
                if isinstance(v, (str, int, float)):
                    val = str(v).strip()
                    if not val:
                        continue
                    if found["code"] is None and "paint_code" in key.replace("-", "_"):
                        if is_valid_code(val):
                            found["code"] = val.upper()
                    elif found["colour"] is None and key in ("colour", "color"):
                        found["colour"] = val
                    elif found["vehicle"] is None and key in ("vehicle_details", "vehicle"):
                        found["vehicle"] = val
                else:
                    visit(v)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(payload)
    return found if found["code"] else None


def is_not_found_text(text: Optional[str]) -> bool:
    """A visible modal is not automatically a miss. The site's own message is
    "Sorry, vehicle XY13FGH not found. Please check entry or try using the
    selector."; a cookie notice or a newsletter box must not be read as one."""
    return bool(text) and any(m in text.lower() for m in NOT_FOUND_MARKERS)


def fresh_cookie_answer(value: Optional[str], baseline: Optional[str],
                        reg: str) -> Optional[dict[str, str]]:
    """A persistent profile keeps `paint_search_data` between runs, and the
    widget restores it on load. Matching `reg_no` is not enough on a repeat of
    the same reg, so the cookie must also have been rewritten since submit."""
    if value is None or value == baseline:
        return None
    return cookie_answer(value, reg)


def rows_answer(rows: list[list[str]]) -> Optional[dict[str, Optional[str]]]:
    out: dict[str, Optional[str]] = {"code": None, "colour": None, "vehicle": None}
    for label, value in rows:
        key = (label or "").strip().rstrip(":").lower()
        val = (value or "").strip()
        if not val or val.upper() in PLACEHOLDERS:
            continue
        if key.startswith("paint code"):
            out["code"] = val.upper() if is_valid_code(val) else None
        elif key.startswith("colour") or key.startswith("color"):
            out["colour"] = val
        elif key.startswith("vehicle"):
            out["vehicle"] = val
    return out if out["code"] else None


# Catalogue prefixes that carry no colour information. L is the VAG lacquer
# prefix (LA7N against A7N). OV and TE are Renault and Dacia catalogue
# prefixes (OV369 against 369, TEGNE against GNE). All three were confirmed
# against codes three or more independent providers agreed on.
CODE_PREFIXES = ("OV", "TE", "L")


def code_stem(x: Optional[str]) -> str:
    if not x:
        return ""
    s = re.sub(r"[^A-Z0-9]", "", str(x).upper())
    for p in sorted(CODE_PREFIXES, key=len, reverse=True):
        # Never strip down to something too short to be distinctive.
        if s.startswith(p) and len(s) - len(p) >= 3:
            return s[len(p):]
    return s


def codes_match(a: Optional[str], b: Optional[str]) -> bool:
    sa, sb = code_stem(a), code_stem(b)
    return bool(sa) and sa == sb


def make_form_key() -> str:
    """Magento generates this client side in form-key-provider.js, 16 random
    alphanumerics, and the widget then posts the same value in both form_key
    and data[form_key]. Writing it ourselves saves waiting for their bundle to
    boot purely to watch it call Math.random."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(16))


def form_key_cookie(value: str) -> dict:
    return {"name": "form_key", "value": value,
            "domain": ".mymotorworld.com", "path": "/"}


def consent_cookie() -> dict:
    """Seeded so the consent banner never renders. Non essential categories are
    declined; nothing is accepted by clicking."""
    val = (f"consentid:{secrets.token_urlsafe(24)},consent:yes,action:yes,"
           "necessary:yes,functional:no,analytics:no,performance:no,"
           f"advertisement:no,other:no,lastRenewedDate:{int(time.time() * 1000)}")
    return {"name": "cookieyes-consent", "value": val,
            "domain": ".mymotorworld.com", "path": "/"}


# ---------------------------------------------------------------------------
# page javascript
# ---------------------------------------------------------------------------

# Knockout is an AMD module here, so it is not reliably on window. Try both.
_KO = """
  (window.ko) || (window.require ? (function () {
      for (const name of ['ko', 'knockout', 'knockoutjs/knockout']) {
          try { const m = window.require(name); if (m && m.dataFor) return m; }
          catch (e) {}
      }
      return null;
  })() : null)
"""

KO_READY_JS = f"""() => {{
    const el = document.querySelector('{SEL_INPUT}');
    if (!el) return {{ready: false, why: 'no input'}};
    const ko = {_KO};
    if (!ko) return {{ready: false, why: 'ko not loaded'}};
    let vm = null;
    try {{ vm = ko.dataFor(el); }} catch (e) {{ return {{ready: false, why: 'dataFor threw'}}; }}
    if (!vm) return {{ready: false, why: 'bindings not applied'}};
    return {{ready: true, has_reg: typeof vm.registrationNo === 'function',
             has_submit: typeof vm.submitRegistration === 'function'}};
}}"""

SET_REG_JS = f"""(reg) => {{
    const el = document.querySelector('{SEL_INPUT}');
    if (!el) return {{ok: false, why: 'no input'}};
    el.focus();
    el.value = reg;
    el.dispatchEvent(new Event('input', {{bubbles: true}}));
    el.dispatchEvent(new Event('change', {{bubbles: true}}));
    const ko = {_KO};
    let observable = null;
    if (ko) {{
        try {{
            const vm = ko.dataFor(el);
            if (vm && ko.isObservable(vm.registrationNo)) {{
                vm.registrationNo(reg);
                observable = vm.registrationNo();
            }}
        }} catch (e) {{}}
    }}
    return {{ok: true, field: el.value, observable: observable}};
}}"""

CLEAR_COOKIE_JS = f"""() => {{
    for (const d of ['', '; domain=.mymotorworld.com', '; domain=www.mymotorworld.com']) {{
        document.cookie = '{COOKIE_NAME}=; Max-Age=0; path=/' + d;
    }}
    return document.cookie.includes('{COOKIE_NAME}');
}}"""

# The widget POSTs this. Calling it from the page costs one round trip and
# skips the whole UI dance: no overlays, no Knockout, no reset, no polling.
# Same origin, same cookies, same session as the form submit it replaces.
FETCH_JS = """async (reg) => {
    // No regex here on purpose: this string passes through python escaping
    // before it is javascript, and \\s does not survive that intact.
    const hit = document.cookie.split(';')
        .map(c => c.trim())
        .find(c => c.startsWith('form_key='));
    if (!hit) return {ok: false, why: 'no form_key cookie'};
    const key = decodeURIComponent(hit.slice('form_key='.length));
    const body = new URLSearchParams();
    body.set('data[registration]', reg);
    body.set('data[form_key]', key);
    body.set('form_key', key);
    try {
        const r = await fetch('%s', {
            method: 'POST',
            headers: {'X-Requested-With': 'XMLHttpRequest',
                      'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'},
            body: body.toString(),
            credentials: 'same-origin',
        });
        return {ok: true, status: r.status, text: (await r.text()).slice(0, 4000)};
    } catch (e) {
        return {ok: false, why: String(e)};
    }
}""" % LOOKUP_PATH

BATCH_FETCH_JS = """async (regs) => {
    const hit = document.cookie.split(';')
        .map(c => c.trim())
        .find(c => c.startsWith('form_key='));
    if (!hit) return {ok: false, why: 'no form_key cookie'};
    const key = decodeURIComponent(hit.slice('form_key='.length));
    const one = async (reg) => {
        const body = new URLSearchParams();
        body.set('data[registration]', reg);
        body.set('data[form_key]', key);
        body.set('form_key', key);
        try {
            const r = await fetch('%s', {
                method: 'POST',
                headers: {'X-Requested-With': 'XMLHttpRequest',
                          'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'},
                body: body.toString(),
                credentials: 'same-origin',
            });
            return {reg: reg, ok: true, status: r.status,
                    text: (await r.text()).slice(0, 4000)};
        } catch (e) {
            return {reg: reg, ok: false, why: String(e)};
        }
    };
    return {ok: true, results: await Promise.all(regs.map(one))};
}""" % LOOKUP_PATH

READ_ROWS_JS = f"""() => Array.from(
    document.querySelectorAll('{SEL_RESULT} .info-row')
).map(r => [
    (r.querySelector('.label') || {{}}).textContent || '',
    (r.querySelector('.value') || {{}}).getAttribute?.('title')
        || (r.querySelector('.value') || {{}}).textContent || ''
])"""


# ---------------------------------------------------------------------------
# browser
# ---------------------------------------------------------------------------

async def _cookie_value(ctx) -> Optional[str]:
    for c in await ctx.cookies():
        if c["name"] == COOKIE_NAME:
            return c["value"]
    return None


async def _wait_for_form_key(ctx, timeout_s: float = 15.0) -> bool:
    """The fetch path needs exactly two things: a document on the origin so the
    call is same origin, and the form_key cookie. Not the images, not the fonts,
    not Google Maps, not Knockout. Waiting for `load` on a retail page costs
    fifteen seconds to obtain something the first response header already gave."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for c in await ctx.cookies():
            if c["name"] == "form_key" and c["value"]:
                return True
        await asyncio.sleep(0.1)
    return False


async def _wait_for_ko(page, debug: bool = False) -> dict:
    """The widget is inert until KO applies its bindings. Interacting before
    that turns go into a native form submit that reloads the page."""
    deadline = time.monotonic() + KO_DEADLINE_S
    state: dict = {"ready": False, "why": "timeout"}
    while time.monotonic() < deadline:
        try:
            state = await page.evaluate(KO_READY_JS)
        except Exception as exc:
            state = {"ready": False, "why": type(exc).__name__}
        if state.get("ready"):
            state["waited_s"] = round(KO_DEADLINE_S - (deadline - time.monotonic()), 2)
            return state
        await asyncio.sleep(0.25)
    if debug:
        print(f"  ko never ready: {state}")
    return state


async def _popup_text(page) -> Optional[str]:
    try:
        loc = page.locator(SEL_POPUP).first
        if await loc.is_visible(timeout=250):
            txt = (await loc.inner_text(timeout=800)).strip()
            return " ".join(txt.split())[:200] or None
    except Exception:
        pass
    return None


async def _clear_overlays(page) -> None:
    """The push prompt and the cookie notice both sit over the widget. The
    notice is info only with no reject control, so it is hidden rather than
    accepted; nothing here clicks a consent button."""
    try:
        await page.evaluate(
            "() => { for (const e of document.querySelectorAll("
            "'.cc-window, .cc-banner')) e.style.display = 'none'; }")
    except Exception:
        pass
    for sel in SEL_OVERLAY_DISMISS:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=300):
                await loc.click()
                return
        except Exception:
            continue


async def _dismiss_popup(page) -> None:
    for sel in SEL_POPUP_CLOSE:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=300):
                await loc.click()
                return
        except Exception:
            continue
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass


async def _page_summary(page) -> dict:
    """Used when the widget is missing. "no input" says the element was not
    there; this says what was there instead, which is the useful half."""
    out = {"title": "", "body": "", "url": page.url}
    try:
        out["title"] = await page.title()
    except Exception:
        pass
    try:
        out["body"] = " ".join((await page.inner_text("body", timeout=3000)).split())[:200]
    except Exception:
        pass
    return out


async def _blocked(page) -> bool:
    try:
        body = (await page.inner_text("body", timeout=2000)).lower()[:2000]
    except Exception:
        return False
    return any(m in body for m in CF_MARKERS)


async def _submit(page) -> bool:
    try:
        go = page.locator(SEL_GO).first
        if await go.is_visible(timeout=1500):
            await go.click()
            return True
    except Exception:
        pass
    try:
        await page.locator(SEL_INPUT).first.press("Enter")
        return True
    except Exception:
        return False


async def _reset(page) -> None:
    try:
        loc = page.locator(SEL_RESET).first
        if await loc.is_visible(timeout=1500):
            await loc.click()
    except Exception:
        pass
    for _ in range(12):
        try:
            if await page.locator(SEL_HAS_VEHICLE).count() == 0:
                return
        except Exception:
            pass
        await asyncio.sleep(0.25)
    await page.goto(URL, wait_until="load")
    await _wait_for_ko(page)


def blank_row(reg: str) -> dict:
    return {"reg": reg, "outcome": "error", "code": None, "colour": None,
            "vehicle": None, "source": None, "detail": None, "ms": 0}


def row_from_response(res: dict, seeded: bool) -> Optional[dict]:
    """One endpoint response to a row, using only the response body. No cookie,
    so this works for several regs answered at once. Returns None when the body
    settles nothing and the caller should fall back."""
    if not res.get("ok"):
        return None
    if res.get("status") != 200:
        if res.get("status") == 403 and not seeded:
            return {"outcome": "blocked", "source": "fetch:json",
                    "detail": "endpoint returned 403"}
        return None  # seeded key refused, or anything else: fall back

    text = res.get("text") or ""
    try:
        ans = search_answer(json.loads(text))
    except Exception:
        ans = None
    if ans:
        return {**ans, "outcome": "ok", "source": "fetch:json"}
    if is_not_found_text(text):
        low = text.lower()
        unknown = "not found" in low and "paints" not in low
        return {"outcome": "unknown_vehicle" if unknown else "not_found",
                "source": "fetch:json", "detail": " ".join(text.split())[:200]}
    return None


async def lookup_batch(page, regs: list[str], seeded: bool,
                       debug: bool) -> dict[str, dict]:
    """Several regs in one round trip. Only the response body is read, so any
    reg whose body settles nothing comes back missing and is retried serially
    by the caller."""
    started = time.monotonic()
    out: dict[str, dict] = {}
    try:
        res = await page.evaluate(BATCH_FETCH_JS, regs)
    except Exception as exc:
        if debug:
            print(f"  batch threw: {type(exc).__name__}")
        return out
    if not res.get("ok"):
        if debug:
            print(f"  batch refused: {res.get('why')}")
        return out
    each = int((time.monotonic() - started) * 1000 / max(len(regs), 1))
    for item in res.get("results", []):
        row = row_from_response(item, seeded)
        if row:
            out[item["reg"]] = {**blank_row(item["reg"]), **row, "ms": each}
    return out


async def _lookup_via_fetch(page, ctx, reg: str, before: Optional[str],
                            debug: bool, seeded: bool = False) -> Optional[dict]:
    """Returns a row update, or None to fall back to driving the form."""
    try:
        res = await page.evaluate(FETCH_JS, reg)
    except Exception as exc:
        if debug:
            print(f"  fetch path threw: {type(exc).__name__}")
        return None
    if debug:
        print(f"  fetch: {str(res)[:220]}")

    row = row_from_response(res, seeded)
    if row:
        return row
    if not res.get("ok") or res.get("status") != 200:
        return None
    # The body settled nothing. The endpoint also sets the cookie on its
    # response, so read that. This is the path that cannot be parallelised:
    # one cookie cannot answer for several regs at once.
    ans = fresh_cookie_answer(await _cookie_value(ctx), before, reg)
    if ans:
        return {**ans, "outcome": "ok", "source": "fetch:cookie"}
    return None


async def lookup_one(page, ctx, reg: str, debug: bool, trace: dict,
                     slow: bool = False) -> dict:
    started = time.monotonic()
    row: dict[str, Any] = blank_row(reg)
    try:
        if not slow:
            before = await _cookie_value(ctx)
            try:
                await page.evaluate(CLEAR_COOKIE_JS)
            except Exception:
                pass
            seeded = trace.get("seeded", False)
            fast = await _lookup_via_fetch(page, ctx, reg, before, debug, seeded)
            if fast:
                row.update(fast)
                return row

            # A seeded key can be refused again later: sessions expire, and a
            # long run outlives one. Recovery is rate limited rather than
            # once-only, so a mid-run expiry does not fail every remaining reg.
            recovered_recently = (time.monotonic() - trace.get("recovered_at", 0)) < 60
            if seeded and not recovered_recently:
                # The invented key was refused. Get a real one from the page
                # once, then this run carries on at full speed.
                if debug:
                    print("  seeded key refused, taking a real session once")
                trace["recovered_at"] = time.monotonic()
                await page.goto(URL, wait_until="commit", timeout=60000)
                await _wait_for_form_key(ctx, 15.0)
                before = await _cookie_value(ctx)
                fast = await _lookup_via_fetch(page, ctx, reg, before, debug)
                if fast:
                    row.update(fast)
                    return row

            if debug:
                print("  fast path gave nothing, loading the page for the form")
            await page.goto(URL, wait_until="load", timeout=60000)

        ko = await _wait_for_ko(page, debug)
        if not ko.get("ready"):
            info = await _page_summary(page)
            row["outcome"] = "blocked" if await _blocked(page) else "error"
            row["detail"] = (f"widget never initialised: {ko.get('why')}; "
                             f"403s={trace['forbidden']}; title={info['title']!r}; "
                             f"body={info['body'][:120]!r}")
            return row
        if debug:
            print(f"  ko ready after {ko.get('waited_s')}s {ko}")

        await _clear_overlays(page)

        # A restored result from a previous run must go before anything is
        # submitted, or it can be read as this reg's answer.
        try:
            if await page.locator(SEL_HAS_VEHICLE).count():
                if debug:
                    print("  restored result on screen, resetting first")
                await _reset(page)
        except Exception:
            pass

        try:
            still_there = await page.evaluate(CLEAR_COOKIE_JS)
            if debug and still_there:
                print("  cookie not removable from js, falling back to change detection")
        except Exception:
            pass
        baseline_cookie = await _cookie_value(ctx)
        try:
            baseline_rows = await page.evaluate(READ_ROWS_JS)
        except Exception:
            baseline_rows = []

        set_state = await page.evaluate(SET_REG_JS, reg)
        if debug:
            print(f"  after set: {set_state}")
        if not set_state.get("ok"):
            row["detail"] = f"could not set reg: {set_state.get('why')}"
            return row
        if set_state.get("observable") not in (None, reg) or set_state.get("field") != reg:
            row["detail"] = (f"field/observable mismatch "
                             f"{set_state.get('field')!r}/{set_state.get('observable')!r}")

        trace["xhr"] = 0
        trace["forbidden"] = 0
        trace["navigated"] = False
        trace["search"] = None
        if not await _submit(page):
            row["detail"] = "no submit control"
            return row

        deadline = time.monotonic() + DEADLINE_S
        resubmitted = False
        spins = 0
        while time.monotonic() < deadline:
            search = trace.get("search")
            if search and search["status"] == 200:
                try:
                    ans = search_answer(json.loads(search["body"]))
                except Exception:
                    ans = None
                if ans:
                    row.update(ans, outcome="ok", source="endpoint")
                    break

            ans = fresh_cookie_answer(await _cookie_value(ctx), baseline_cookie, reg)
            if ans:
                row.update(ans, outcome="ok", source="cookie")
                break

            if trace["forbidden"]:
                cf = trace.get("cf") or {}
                row.update(outcome="blocked",
                           detail=f"{trace['forbidden']} same origin 403 "
                                  f"({cf.get('cf-mitigated') or cf.get('server') or 'edge'})")
                break

            try:
                if await page.locator(SEL_HAS_VEHICLE).count():
                    rows = await page.evaluate(READ_ROWS_JS)
                    if rows and rows != baseline_rows:
                        ans = rows_answer(rows)
                        if ans:
                            row.update(ans, outcome="ok", source="dom")
                            break
            except Exception:
                pass

            spins += 1
            popup = await _popup_text(page) if spins % POPUP_EVERY == 0 else None
            if popup:
                if is_not_found_text(popup):
                    row.update(outcome="not_found", detail=popup, source="popup")
                    await _dismiss_popup(page)
                    break
                # something else on screen, clear it and keep waiting
                if debug:
                    print(f"  dismissing unrelated overlay: {popup[:80]}")
                await _dismiss_popup(page)

            if trace.get("navigated"):
                # Native submit: KO was not bound, or it rebound mid click.
                row.update(outcome="error", detail="page navigated after submit")
                break

            if not resubmitted and (deadline - time.monotonic()) < (DEADLINE_S - RESUBMIT_AFTER_S):
                resubmitted = True
                if debug:
                    print(f"  no answer after {RESUBMIT_AFTER_S}s, resubmitting "
                          f"(xhr so far: {trace['xhr']})")
                await page.evaluate(SET_REG_JS, reg)
                await _submit(page)

            await asyncio.sleep(POLL_S)
        else:
            if await _blocked(page):
                row.update(outcome="blocked", detail="challenge page")
            else:
                row["outcome"] = "timeout"
                row["detail"] = (f"no answer, {trace['xhr']} same origin xhr "
                                 f"after submit")

    except Exception as exc:
        row.update(outcome="error", detail=type(exc).__name__)
    finally:
        row["ms"] = int((time.monotonic() - started) * 1000)
    return row


async def _debug_report(page, ctx, trace: dict) -> None:
    print("\n--- debug ---")
    print(f"url now: {page.url}")
    print(f"navigated after submit: {trace.get('navigated')}")
    print(f"same origin xhr after submit: {trace['xhr']}, "
          f"403s: {trace['forbidden']} {trace.get('cf') or ''}")
    if trace.get("search"):
        print(f"lookup endpoint: {trace['search']['status']} "
              f"{trace['search']['body'][:400]}")
    for c in trace["calls"][-15:]:
        print(f"  {c['method']:4} {c['status']} {c['url'][:110]}")
        if c.get("post"):
            print(f"       post: {c['post'][:200]}")
        if c.get("body"):
            print(f"       body: {c['body'][:300]}")
    if trace["console"]:
        print("console errors:")
        for m in trace["console"][:10]:
            print(f"  {m[:200]}")
    names = [c["name"] for c in await ctx.cookies()]
    print(f"cookies: {sorted(names)}")
    try:
        html = await page.eval_on_selector(
            "#paintMatchingVrmLookup", "e => e.outerHTML")
        print("widget now:", re.sub(r"\s+", " ", html)[:1200])
    except Exception as exc:
        print(f"widget outerHTML unavailable: {exc}")
    Path("debug_page.html").write_text(await page.content(), encoding="utf-8")
    await page.screenshot(path="debug_page.png", full_page=False)
    print("wrote debug_page.html and debug_page.png")


async def run(regs: list[str], headed: bool, out: Optional[Path], debug: bool,
              profile: Optional[str], chromium_only: bool,
              block_assets: bool, offscreen: bool, slow: bool,
              delay: Optional[float], concurrency: int) -> list[dict]:
    from playwright.async_api import async_playwright

    rows: list[dict] = []
    timings: dict = {"launch": 0.0, "page": 0.0, "driver": 0.0, "close": 0.0,
                     "via": "full page"}
    started_all = time.monotonic()
    trace: dict = {"xhr": 0, "forbidden": 0, "navigated": False,
                   "calls": [], "console": [], "cf": {}, "search": None}

    async with async_playwright() as p:
        # --enable-automation and navigator.webdriver are read by bot
        # management. Real Chrome is preferred over bundled Chromium for the
        # same reason; fall back if it is not installed.
        t_launch = time.monotonic()
        args = ["--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled"]
        if offscreen:
            # A genuine headed browser parked off the visible desktop. Nothing
            # to fingerprint, because nothing about it is headless.
            args.append("--window-position=-32000,-32000")
        launch: dict = {
            "headless": False if offscreen else not headed,
            "args": args,
            "ignore_default_args": ["--enable-automation"],
        }
        if not chromium_only:
            launch["channel"] = "chrome"
        if profile:
            try:
                ctx = await p.chromium.launch_persistent_context(
                    profile, locale="en-GB", timezone_id="Europe/London",
                    viewport={"width": 1400, "height": 1000}, **launch)
            except Exception:
                launch.pop("channel", None)
                print("real Chrome not available, falling back to bundled chromium")
                ctx = await p.chromium.launch_persistent_context(
                    profile, locale="en-GB", timezone_id="Europe/London",
                    viewport={"width": 1400, "height": 1000}, **launch)
            browser = None
        else:
            try:
                browser = await p.chromium.launch(**launch)
            except Exception:
                launch.pop("channel", None)
                print("real Chrome not available, falling back to bundled chromium")
                browser = await p.chromium.launch(**launch)
            opts = {"locale": "en-GB", "timezone_id": "Europe/London",
                    "viewport": {"width": 1400, "height": 1000}}
            ctx = await browser.new_context(**opts)
            if launch["headless"]:
                # Headless Chrome puts "HeadlessChrome" in the UA of every
                # request. Read the real one and rebuild the context without it,
                # rather than hardcoding a version that will drift.
                scratch = await ctx.new_page()
                ua = await scratch.evaluate("navigator.userAgent")
                await scratch.close()
                if "Headless" in ua:
                    await ctx.close()
                    ctx = await browser.new_context(
                        user_agent=ua.replace("HeadlessChrome", "Chrome"), **opts)
                    if debug:
                        print(f"  ua cleaned: {ua.replace('HeadlessChrome', 'Chrome')}")

        timings["launch"] = time.monotonic() - t_launch
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        await ctx.add_cookies([consent_cookie()])

        async def kill(route):
            await route.abort()

        await ctx.route(PUSH_HOSTS, kill)

        # Interception rewrites every request, which is itself a signal. Off by
        # default now; --block turns the tracker filtering back on.
        if block_assets:
            async def block(route, request):
                if request.resource_type in ("image", "media", "font"):
                    await route.abort()
                elif any(f in request.url for f in BLOCK_FRAGMENTS):
                    await route.abort()
                else:
                    await route.continue_()

            await ctx.route("**/*", block)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        pending: list[asyncio.Task] = []

        async def on_response(resp):
            req = resp.request
            if ORIGIN not in req.url:
                return
            if resp.status == 403:
                trace["forbidden"] += 1
                if not trace.get("cf"):
                    h = resp.headers
                    trace["cf"] = {k: h.get(k) for k in
                                   ("cf-mitigated", "cf-ray", "server")
                                   if h.get(k)}
            if req.resource_type not in ("xhr", "fetch"):
                return
            trace["xhr"] += 1

            # The fast path already has the body from the page. Re-reading it
            # over CDP is a second copy of every lookup response for nothing,
            # so only do it when the form path or --debug will actually use it.
            if not (debug or slow):
                return

            text = None
            try:
                text = await resp.text()
            except Exception:
                pass

            if LOOKUP_PATH in req.url:
                trace["search"] = {"status": resp.status, "body": (text or "")[:4000]}

            if not debug:
                return
            body = text[:1500] if text and text.strip()[:1] in "{[" else None
            trace["calls"].append({"method": req.method, "url": req.url,
                                   "status": resp.status, "post": req.post_data,
                                   "body": body})

        page.on("response", lambda r: pending.append(asyncio.create_task(on_response(r))))
        page.on("framenavigated", lambda f: trace.__setitem__("navigated", True)
                if f == page.main_frame else None)
        page.on("console", lambda m: trace["console"].append(f"{m.type}: {m.text}")
                if m.type in ("error", "warning") else None)

        t_page = time.monotonic()
        if slow:
            await page.goto(URL, wait_until="load", timeout=60000)
        else:
            timings["via"] = "seeded"
            if not await _wait_for_form_key(ctx, 0.05):
                await ctx.add_cookies([form_key_cookie(make_form_key())])
            try:
                await page.goto(LIGHT_URL, wait_until="commit", timeout=20000)
            except Exception:
                pass
            got_key = await _wait_for_form_key(ctx, 2.0)
            if not got_key:
                # The light document did not establish a session; pay for the
                # real page once.
                timings["via"] = "full page"
                await page.goto(URL, wait_until="commit", timeout=60000)
                got_key = await _wait_for_form_key(ctx, 15.0)
            try:
                # Abandon whatever is still downloading. Bounded, because this
                # can block until an execution context exists.
                await asyncio.wait_for(page.evaluate("window.stop()"), 2.0)
            except Exception:
                pass
            trace["seeded"] = timings["via"] == "seeded"
            if not got_key:
                print("no form_key cookie, falling back to the form path")
                slow = True
                await page.goto(URL, wait_until="load", timeout=60000)
        timings["page"] = time.monotonic() - t_page
        trace["navigated"] = False

        if slow and await _blocked(page):
            print("challenge page on load. Run once with --headed --profile .profile, "
                  "clear it by hand, then rerun; the clearance persists.")
            await (browser.close() if browser else ctx.close())
            return rows

        if slow:
            try:
                if await page.locator(SEL_HAS_VEHICLE).count():
                    await _reset(page)
            except Exception:
                pass

        def emit(n: int, row: dict) -> None:
            rows.append(row)
            print(f"[{n}/{len(regs)}] {row['reg']:<8} {row['outcome']:<10} "
                  f"{row['code'] or '':<8} {row['ms']:>5}ms  "
                  f"{row['vehicle'] or row['detail'] or ''}")
            if out:
                _append_csv(out, row)

        async def pause() -> None:
            lo, hi = DELAY_S if delay is None else (delay * 0.75, delay * 1.25)
            await asyncio.sleep(random.uniform(lo, hi))

        n = 0
        queue = list(regs)
        stopped = False

        # The first reg always runs on its own. Whether its answer came out of
        # the response body or out of the cookie decides if the rest can be
        # batched, and guessing that would be guessing.
        while queue and not stopped:
            batch_size = 1
            if concurrency > 1 and trace.get("json_ok") and not slow:
                batch_size = min(concurrency, len(queue))

            if batch_size == 1:
                reg = queue.pop(0)
                n += 1
                row = await lookup_one(page, ctx, reg, debug, trace, slow)
                if row["outcome"] in ("error", "timeout"):
                    # One retry. A blip should not cost a reg, and the outcome
                    # is recorded either way so a real fault still shows.
                    if debug:
                        print(f"  {row['outcome']} on {reg}, retrying once")
                    await asyncio.sleep(1.0)
                    retry = await lookup_one(page, ctx, reg, debug, trace, slow)
                    if retry["outcome"] not in ("error", "timeout"):
                        row = retry
                if row.get("source") == "fetch:json":
                    trace["json_ok"] = True
                emit(n, row)
                if debug:
                    await _debug_report(page, ctx, trace)
                if row["outcome"] == "blocked":
                    print("stopping: challenged mid run")
                    stopped = True
                    break
                if not (row.get("source") or "").startswith("fetch"):
                    await _reset(page)
                if slow and n % RELOAD_EVERY == 0:
                    await page.goto(URL, wait_until="load")
                    await _wait_for_ko(page)
            else:
                chunk = [queue.pop(0) for _ in range(batch_size)]
                got = await lookup_batch(page, chunk, trace.get("seeded", False), debug)
                for reg in chunk:
                    row = got.get(reg)
                    if row is None:
                        # Body settled nothing for this one; it earns a serial
                        # run rather than being written off.
                        row = await lookup_one(page, ctx, reg, debug, trace, slow)
                    n += 1
                    emit(n, row)
                    if row["outcome"] == "blocked":
                        print("stopping: challenged mid run")
                        stopped = True
                        break

            if queue and not stopped:
                await pause()

        t_close = time.monotonic()
        await asyncio.gather(*pending, return_exceptions=True)
        timings["close"] = time.monotonic() - t_close
        lookups = sum(r["ms"] for r in rows) / 1000
        total = time.monotonic() - started_all
        print(f"\ntiming: driver {timings['driver']:.1f}s, browser "
              f"{timings['launch']:.1f}s, first page {timings['page']:.1f}s "
              f"via {timings['via']}, lookups {lookups:.1f}s "
              f"({lookups / max(len(rows), 1):.1f}s each), drain "
              f"{timings['close']:.1f}s, total {total:.1f}s")
        if browser:
            await browser.close()
        else:
            await ctx.close()
    return rows


FIELDS = ["reg", "outcome", "code", "colour", "vehicle", "source", "detail", "ms"]


def _append_csv(path: Path, row: dict) -> None:
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k) for k in FIELDS})


# utf-8-sig everywhere: PowerShell writes a BOM with -Encoding utf8, which
# turns the first column name into "\ufeffreg" and silently drops every row.
# Outcomes that are an answer. Anything else is the run failing, not the site
# answering, and must be retried on the next run rather than skipped forever.
CONCLUSIVE = {"ok", "not_found", "unknown_vehicle"}


def _done_regs(path: Path) -> set[str]:
    """Only regs the site actually answered for. A reg that timed out or hit a
    network blip stays in the queue; skipping it because a row exists would
    quietly drop it from every future run."""
    if not path.exists():
        return set()
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return {r["reg"] for r in csv.DictReader(fh)
                if r.get("reg") and (r.get("outcome") or "") in CONCLUSIVE}


REG_HEADERS = {"reg", "registration", "reg_no", "regno", "vrm", "plate"}
CODE_HEADERS = {"code", "paint_code", "paintcode", "expected", "expected_code"}


def _load_expected(path: Path) -> dict[str, str]:
    """Deliberately forgiving about shape: a header line is optional, the
    column names have several accepted spellings, and extra columns are fine.
    Getting this wrong costs a confusing empty run, not a wrong answer."""
    rows = [r for r in csv.reader(path.read_text(encoding="utf-8-sig").splitlines())
            if any((c or "").strip() for c in r)]
    if not rows:
        return {}

    head = [(c or "").strip().lower() for c in rows[0]]
    if any(h in REG_HEADERS for h in head):
        reg_i = next(n for n, h in enumerate(head) if h in REG_HEADERS)
        code_i = next((n for n, h in enumerate(head) if h in CODE_HEADERS),
                      1 if len(head) > 1 else 0)
        body = rows[1:]
    else:
        reg_i, code_i, body = 0, 1, rows  # no header, take it positionally

    out: dict[str, str] = {}
    for r in body:
        if len(r) <= max(reg_i, code_i):
            continue
        reg = normalise_reg(r[reg_i])
        code = (r[code_i] or "").strip().upper()
        if reg and code:
            out[reg] = code
    return out


def _report(rows: list[dict], expected: dict[str, str]) -> None:
    agree = disagree = missing = 0
    print("\nreg       expected   returned   verdict")
    for r in rows:
        exp = expected.get(r["reg"])
        if not exp:
            continue
        got = r["code"]
        if not got:
            missing += 1
            verdict = f"no answer ({r['outcome']})"
        elif codes_match(exp, got):
            agree += 1
            verdict = "match" if exp == got else "match (prefix differs)"
        else:
            disagree += 1
            verdict = "MISMATCH"
        print(f"{r['reg']:<9} {exp:<10} {got or '':<10} {verdict}")
    print(f"\n{agree} agree, {disagree} disagree, {missing} no answer")
    if disagree:
        print("a single disagreement against a dealer confirmed code means this "
              "is a cross check, not a code source")


def selftest() -> int:
    fails: list[str] = []

    def check(label, cond):
        if not cond:
            fails.append(label)
            print(f"FAIL  {label}")

    for good in ("LA7N", "A7N", "Z9Y", "OVDQH", "OV369", "OVKQM", "Z1/A7N"):
        check(f"accepts {good}", is_valid_code(good))
    for bad in ("", None, "-", "ENTER REG", "enter reg", "a",
                "Please enter your registration number"):
        check(f"rejects {bad!r}", not is_valid_code(bad))

    live = ("%7B%22vehicle_details%22%3A%22Audi%20A3%202009%202.0%20Diesel%22%2C"
            "%22colour%22%3A%22BLACK%22%2C%22paint_code%22%3A%22Z9Y%22%2C"
            "%22reg_no%22%3A%22wp09uou%22%7D")
    check("live cookie parses", cookie_answer(live, "WP09UOU") ==
          {"code": "Z9Y", "colour": "BLACK", "vehicle": "Audi A3 2009 2.0 Diesel"})
    check("cookie tolerates spaced reg", cookie_answer(live, "wp09 uou") is not None)
    check("cookie for another reg is refused", cookie_answer(live, "GM14DKE") is None)
    check("empty cookie is refused", cookie_answer(None, "WP09UOU") is None)
    check("garbage cookie is refused", cookie_answer("%7Bnot json", "WP09UOU") is None)
    check("cookie with placeholder code refused",
          cookie_answer(urllib.parse.quote(json.dumps(
              {"paint_code": "ENTER REG", "reg_no": "AB12CDE"})), "AB12CDE") is None)

    check("live dom rows parse", rows_answer(
        [["Vehicle:", "Volkswagen Golf 2014 1.6 Diesel"],
         ["Colour:", "GREY"], ["Paint Code:", "A7N"]]) ==
        {"code": "A7N", "colour": "GREY", "vehicle": "Volkswagen Golf 2014 1.6 Diesel"})
    check("dom rows without a code yield nothing",
          rows_answer([["Vehicle:", "VW Golf"], ["Colour:", "GREY"]]) is None)
    check("dom placeholder yields nothing", rows_answer([["Paint Code:", "ENTER REG"]]) is None)

    check("site miss message reads as not found",
          is_not_found_text("Sorry, vehicle XY13FGH not found. Please check "
                            "entry or try using the selector."))
    check("cookie notice does not read as not found",
          not is_not_found_text("Our site uses cookies to give you the best "
                                "shopping experience. Continue if you're happy."))
    check("push prompt does not read as not found",
          not is_not_found_text("Subscribe to our notifications for the latest "
                                "offers & deals. You can disable anytime."))
    check("empty modal does not read as not found", not is_not_found_text(""))
    check("popup selector excludes role=dialog", 'role="dialog"' not in SEL_POPUP)
    check("popup selector is visible scoped", SEL_POPUP.count(":visible") == 5)

    check("stale cookie identical to baseline is refused",
          fresh_cookie_answer(live, live, "WP09UOU") is None)
    check("rewritten cookie is accepted",
          fresh_cookie_answer(live, "something-older", "WP09UOU") is not None)
    check("no cookie is refused", fresh_cookie_answer(None, None, "WP09UOU") is None)
    check("fresh cookie for the wrong reg is still refused",
          fresh_cookie_answer(live, None, "GM14DKE") is None)

    check("endpoint payload parses", search_answer(
        {"paint_search_data": {"vehicle_details": "Volkswagen Golf 2014 1.6 Diesel",
                               "colour": "GREY", "paint_code": "A7N"}}) ==
        {"code": "A7N", "colour": "GREY", "vehicle": "Volkswagen Golf 2014 1.6 Diesel"})
    check("endpoint failure payload yields nothing",
          search_answer({"success": False, "message": "No vehicle found"}) is None)
    check("endpoint placeholder yields nothing",
          search_answer({"paint_code": "ENTER REG"}) is None)
    check("endpoint list payload", search_answer(
        [{"data": [{"paint_code": "OVDQH"}]}])["code"] == "OVDQH")

    check("renault prefix OV369/369", codes_match("OV369", "369"))
    check("renault prefix TEGNE/GNE", codes_match("TEGNE", "GNE"))
    check("dacia scheme difference is not a match",
          not codes_match("TERQH", "141D7N"))
    check("dacia scheme difference is not a match either",
          not codes_match("TEFAA", "141DC3"))
    check("ford scheme difference is not a match",
          not codes_match("PN4A7", "7236/BRQA"))
    check("ford exact still matches", codes_match("PN4GM", "PN4GM"))
    check("prefix is not stripped below three chars", not codes_match("TEG", "G"))
    check("vehicle miss is separated from paint miss",
          is_not_found_text("Sorry, vehicle NHA64P not found.") and
          is_not_found_text("Sorry, couldn't find paints for LG73FAC."))

    check("VAG prefix match LA7N/A7N", codes_match("LA7N", "A7N"))
    check("VAG prefix match LZ9Y/Z9Y", codes_match("LZ9Y", "Z9Y"))
    check("exact still matches", codes_match("OVDQH", "OVDQH"))
    check("separators ignored", codes_match("Z1/A7N", "Z1 A7N"))
    check("different codes do not match", not codes_match("LA7N", "LB9A"))
    check("empty never matches", not codes_match(None, "A7N"))
    check("short L code is not stripped", not codes_match("LB9", "B9"))

    check("reg normalise", normalise_reg(" ab-12 cde ") == "AB12CDE")
    check("reg normalise empty", normalise_reg(None) == "")

    # the js is built by f-string, so a broken selector constant shows up here
    keys = {make_form_key() for _ in range(200)}
    check("form key is 16 chars", all(len(k) == 16 for k in keys))
    check("form key is alphanumeric", all(k.isalnum() for k in keys))
    check("form key is not repeated", len(keys) == 200)
    check("form key cookie is scoped to the site",
          form_key_cookie("x")["domain"] == ".mymotorworld.com")

    hit = row_from_response({"ok": True, "status": 200, "text": json.dumps(
        {"paint_code": "A7N", "colour": "GREY",
         "vehicle_details": "Volkswagen Golf 2014"})}, False)
    import tempfile as _tf
    with _tf.TemporaryDirectory() as _d:
        _f = Path(_d) / "r.csv"
        _f.write_text("reg,outcome,code,colour,vehicle,source,detail,ms\n"
                      "A1,ok,A7N,,,fetch:json,,1\n"
                      "B2,not_found,,,,fetch:json,,1\n"
                      "C3,unknown_vehicle,,,,fetch:json,,1\n"
                      "D4,timeout,,,,,,1\n"
                      "E5,error,,,,,,1\n"
                      "F6,blocked,,,,,,1\n", encoding="utf-8")
        check("resume skips answered regs", _done_regs(_f) == {"A1", "B2", "C3"})
        check("resume retries failed regs",
              not ({"D4", "E5", "F6"} & _done_regs(_f)))

    check("json body answers", hit and hit["source"] == "fetch:json"
          and hit["code"] == "A7N")
    miss = row_from_response({"ok": True, "status": 200, "text": json.dumps(
        {"error": "Sorry, couldn't find paints for AB12CDE."})}, False)
    check("paint miss is not_found", miss and miss["outcome"] == "not_found")
    gone = row_from_response({"ok": True, "status": 200, "text": json.dumps(
        {"error": "Sorry, vehicle AB12CDE not found."})}, False)
    check("vehicle miss is unknown_vehicle", gone
          and gone["outcome"] == "unknown_vehicle")
    check("empty body falls back", row_from_response(
        {"ok": True, "status": 200, "text": "{}"}, False) is None)
    check("seeded 403 falls back rather than reporting blocked",
          row_from_response({"ok": True, "status": 403, "text": ""}, True) is None)
    check("unseeded 403 reports blocked", row_from_response(
        {"ok": True, "status": 403, "text": ""}, False)["outcome"] == "blocked")
    check("batch js targets the endpoint", LOOKUP_PATH in BATCH_FETCH_JS)
    check("batch js is parallel", "Promise.all" in BATCH_FETCH_JS)

    check("fetch js targets the endpoint", LOOKUP_PATH in FETCH_JS)
    check("fetch js sends both form key fields",
          "data[form_key]" in FETCH_JS and "form_key" in FETCH_JS)
    check("fetch js keeps the session", "same-origin" in FETCH_JS)

    for js in (KO_READY_JS, SET_REG_JS, READ_ROWS_JS, FETCH_JS):
        check("js has no unresolved braces", "{{" not in js and "}}" not in js)
    check("ko probe references the input", SEL_INPUT in KO_READY_JS)

    import tempfile
    want = {"GM14DKE": "LA7N", "WP09UOU": "LZ9Y"}
    shapes = {
        "bom header": "\ufeffreg,code\nGM14DKE,LA7N\nWP09UOU,LZ9Y\n",
        "plain header": "reg,code\nGM14DKE,LA7N\nWP09UOU,LZ9Y\n",
        "no header": "GM14DKE,LA7N\nWP09UOU,LZ9Y\n",
        "alt names": "registration,paint_code\nGM14DKE,LA7N\nWP09UOU,LZ9Y\n",
        "extra columns": ("reg,make,code,notes\nGM14DKE,VW,LA7N,dealer\n"
                          "WP09UOU,Audi,LZ9Y,dealer\n"),
        "spaced regs": "reg,code\ngm14 dke,la7n\nWP09 UOU,lz9y\n",
        "blank lines": "reg,code\n\nGM14DKE,LA7N\n\nWP09UOU,LZ9Y\n\n",
    }
    with tempfile.TemporaryDirectory() as d:
        for label, text in shapes.items():
            f = Path(d) / "v.csv"
            f.write_text(text, encoding="utf-8")
            check(f"expected csv, {label}", _load_expected(f) == want)
        f = Path(d) / "empty.csv"
        f.write_text("", encoding="utf-8")
        check("empty csv yields nothing", _load_expected(f) == {})

    print()
    if fails:
        print(f"{len(fails)} FAILURES")
        return 1
    print("selftest green")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="reg to paint code")
    ap.add_argument("regs", nargs="*")
    ap.add_argument("--file", help="text file, one reg per line")
    ap.add_argument("--out", default="results.csv")
    ap.add_argument("--expect", help="csv with reg,code columns for an accuracy run")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--profile", help="persistent chrome profile dir, keeps clearance")
    ap.add_argument("--debug", action="store_true", help="full diagnostic report")
    ap.add_argument("--slow", action="store_true",
                    help="drive the form instead of calling the endpoint directly")
    ap.add_argument("--concurrency", type=int, default=1,
                    help="regs per round trip once the endpoint is proven to "
                         "answer in its response body (default 1)")
    ap.add_argument("--delay", type=float, default=None,
                    help="mean seconds between lookups (default 3, jittered)")
    ap.add_argument("--offscreen", action="store_true",
                    help="real headed browser positioned off the desktop, "
                         "invisible without being headless")
    ap.add_argument("--chromium", action="store_true",
                    help="use bundled chromium instead of installed Chrome")
    ap.add_argument("--block", action="store_true",
                    help="intercept and block trackers, faster but more detectable")
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    expected: dict[str, str] = {}
    regs = [normalise_reg(r) for r in a.regs]

    if a.expect:
        path = Path(a.expect)
        if not path.exists():
            print(f"{path} not found. Write it first: two columns, one car per "
                  f"line, where code is the dealer confirmed answer you are "
                  f"checking against, not one this site gave you.\n\n"
                  f"reg,code\nGM14DKE,LA7N\n")
            return 2
        expected = _load_expected(path)
        if not expected:
            print(f"{path} has no usable rows. It needs a header line with "
                  f"reg and code columns.")
            return 2
        regs += list(expected)

    if a.file:
        path = Path(a.file)
        if not path.exists():
            print(f"{path} not found. It is a plain text file, one reg per line.")
            return 2
        regs += [normalise_reg(ln) for ln in
                 path.read_text(encoding="utf-8-sig").splitlines()]
    regs = [r for r in dict.fromkeys(regs) if 2 <= len(r) <= 8]
    if not regs:
        ap.print_help()
        return 2

    out = Path(a.out) if (a.file or a.expect or len(regs) > 1) else None
    if out and not a.no_resume:
        done = _done_regs(out)
        if any(r in done for r in regs):
            print(f"skipping {sum(1 for r in regs if r in done)} already in {out}")
        regs = [r for r in regs if r not in done]
    if not regs:
        print("nothing to do")
        return 0

    rows = asyncio.run(run(regs, a.headed, out, a.debug, a.profile,
                           a.chromium, a.block, a.offscreen, a.slow, a.delay,
                           max(1, a.concurrency)))
    if expected:
        _report(rows, expected)
    elif len(rows) == 1 and not out:
        print(json.dumps(rows[0], indent=2))
    else:
        ok = sum(1 for r in rows if r["outcome"] == "ok")
        srcs = collections.Counter(r.get("source") or "-" for r in rows)
        print(f"{ok}/{len(rows)} ok  ->  {out}   "
              f"({', '.join(f'{n} {k}' for k, n in srcs.most_common())})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
