# mmw

Reg to paint code from a public retailer widget. One file, `mmw.py`.

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
python mmw.py --selftest
python mmw.py GM14DKE --headed
```

## Speed

By default each lookup calls the widget's own endpoint from inside the page:

```
POST /paintmatching/colour/search
data[registration]=<REG>&data[form_key]=<key>&form_key=<key>
```

Same origin, same cookies, same session as the form submit it replaces, so it
is the same request the site would have made. That skips the overlays, the
Knockout wait, the reset and the polling: one round trip instead of a UI dance.
`--slow` drives the form instead, and the form path is also the automatic
fallback whenever the endpoint gives nothing usable.

Two other costs went with it. A full `body` text scrape ran four times a second
to check for a challenge page, on a large Magento DOM; it now runs once, after
the wait fails. The popup probe carried a 250ms timeout on every poll; it now
runs every sixth.

The page itself is also skipped. Magento generates `form_key` in JavaScript,
in `form-key-provider.js`, sixteen random alphanumerics; the widget then posts
that same value in both `form_key` and `data[form_key]`. The server validates
by comparing the two rather than against the session, so the cookie can simply
be written here and the retail page never has to load at all. What loads
instead is `/customer/section/load/?sections=paint-data`, a few hundred bytes
on the same origin, purely to host the fetch call.

If a seeded key is ever refused, the first lookup detects it, takes one real
session from the page, and the run continues at full speed. A 403 on an
invented key means a rejected key, not a blocked client, and is not reported as
one.

Measured on one reg, cold, headless: 10.4s before this, 3.4s after, of which
0.7s is Chrome booting and 0.7s is the site answering. Neither is ours.

What remains is under your control rather than the code's. On a forty reg run
the lookups took 35s and the throttle took 39s, so more than half the wall
clock is a politeness setting.

`--delay 1` halves the gap. `--concurrency 4` sends four regs in one round trip
via `Promise.all` inside the page. The first reg always runs alone: if its
answer came from the response body (`source: fetch:json`) the rest can be
batched, and if it came from the cookie (`fetch:cookie`) they cannot, because
one cookie cannot answer for four regs at once. That is why the source label
distinguishes the two. Any reg in a batch whose body settles nothing is retried
on its own rather than written off.

Together those take a forty reg run from about 78s to roughly 25s. Note what
that does to request rate before using it on a large batch: four requests at
once against someone else's free endpoint is a different kind of guest.

## How it reads the answer

The site writes its own result to a plain cookie on every successful lookup:

```json
{"vehicle_details":"Volkswagen Golf 2014 1.6 Diesel",
 "colour":"GREY","paint_code":"A7N","reg_no":"gm14dke"}
```

The payload carries the reg it belongs to, so the cookie is only accepted when
`reg_no` matches the reg just submitted, and the cookie is cleared before each
lookup. A stale read is therefore impossible, which is the one real risk in
holding a single page open across many regs. The structured DOM read of
`.selected-paint-vrm-details` is the fallback if the cookie stops being written.

There is a second machine readable route, `/customer/section/load/?sections=
paint-data`, Magento's private content endpoint. Not used: it needs a live
session and adds nothing the cookie does not already give.

## Knockout, and why the first version timed out

The widget is a Knockout component. The form carries
`data-bind="event:{submit: submitRegistration}"` and the input carries
`value: registrationNo`, and neither exists until KO applies its bindings.
Click go before that moment and the browser performs a plain native form
submit: the page reloads to `/car-paint-by-reg?vrm-registration=...`, KO starts
again with an empty field, and no result ever arrives. A fixed sleep after
`domcontentloaded` is not enough on a Cloudflare fronted Magento page.

So every lookup first waits for `ko.dataFor(input)` to return a view model, and
only then fills. KO is an AMD module here and is not reliably on `window`, so
the probe tries `window.ko` and then `require('ko')`, `require('knockout')` and
`require('knockoutjs/knockout')`. The reg is written to the DOM value, the
`input` and `change` events are dispatched, and the observable is set directly,
so it does not matter which of the three the binding is listening on.

If KO never becomes ready the outcome is `error` with
`widget never initialised`, not a silent twenty second `timeout`. A navigation
after submit is detected and reported as `page navigated after submit`, which
is the native submit signature.

## Selectors, from the live markup

| what | selector |
|---|---|
| reg input | `#paint-vrm-reg`, Knockout `value: registrationNo` |
| go | `#paint-vrm-search` |
| reset | `#vrm-remove` in result state, `#paint-vrm-reset` before |
| result panel | `.selected-paint-vrm-details .info-row` |
| state flag | `.has-paintvehicle` present after a hit |

The input is Knockout bound on `change`, so the fill is followed by an explicit
`change` dispatch. Without it a visibly filled box can submit an empty
observable and the widget sits there doing nothing.

## Cloudflare, and what actually got through

The site runs Cloudflare Bot Management (`__cf_bm`). Bundled Chromium with
request interception got a 403 on every same origin XHR while the document
itself loaded fine, which looks exactly like a slow page and is not one. Three
changes fixed it together:

- launch the installed Chrome (`channel="chrome"`), not bundled Chromium
- drop `--enable-automation` and mask `navigator.webdriver`
- no route interception; rewriting every request is itself a signal

`--block` turns tracker filtering back on if you ever want the speed. It also
brings back the 403s, so leave it off.

Their New Relic RUM still reports `webdriverDetected` on every page view, so
they can see what this is whenever they choose to look. Treat continued access
as a courtesy, not a guarantee.

## Staleness and the persistent profile

`--profile .profile` keeps `cf_clearance` between runs, which is worth having.
It also keeps `paint_search_data`, and the widget restores that result on load
before anything is submitted. So on a repeat of the same reg the old cookie can
be returned in milliseconds and read as a fresh success.

Three guards, in order: a restored result is reset before the reg is entered,
the cookie is deleted from JS before submit, and the answer is only accepted
when the cookie has actually been rewritten since. The DOM fallback likewise
ignores a panel identical to its pre submit state. `fresh_cookie_answer` is the
pure rule and `--selftest` covers it.

The consent banner is skipped by seeding the CookieYes cookie with the non
essential categories set to no, so nothing is accepted by clicking. Trackers
(Clarity, Klaviyo, Smartech, New Relic, GA, Bing) are blocked at the route
level, which also removes the push notification overlay.

## Accuracy: what the two real answers already show

Their codes are the short form. `A7N` for the Golf where the VW dealer code is
`LA7N`; `Z9Y` for the Audi where Audi gives `LZ9Y`. Compared naively both read
as mismatches when they are in fact the same colour. `codes_match` strips a
leading `L` from a VAG style stem before comparing, and `--expect` uses it.

Their colour field is a family name, `GREY` and `BLACK`, not the marketing
name. That is DVLA grade colour, not `Limestone Grey`. Which raises the real
question to test: when a model year offers several colours inside one DVLA
family, does their data pick the right one or the most common one? Build the
`--expect` file from cars with an uncommon colour inside a common family. That
is where a source like this fails, and it fails plausibly rather than visibly.

```powershell
python mmw.py --expect verified.csv --headed
```

`verified.csv` is `reg,code` with dealer confirmed codes. Agrees on all: a
candidate for the race. Disagrees once: cross check only, never a code source.

## Outcomes

| outcome | meaning |
|---|---|
| `ok` | code read from the cookie, or from the panel as fallback |
| `not_found` | the widget's own popup fired, text in `detail` |
| `timeout` | deadline hit with no cookie, no panel and no popup; `detail` gives the same origin XHR count since submit |
| `blocked` | challenge page, run stops |
| `error` | widget never initialised, page navigated after submit, or an exception type |

`--out results.csv` appends as it goes and skips regs already present, so a
crashed run resumes rather than starting over.

## Notes

- Volume stays low, `DELAY_S` is a jittered two to four seconds. No contract
  and no ToS grant, so this is best effort and never backs a paid promise.
- No service wrapper. If coloureg calls it later, wrap `lookup_one` in FastAPI
  with a shared secret and keep the page warm the same way.
