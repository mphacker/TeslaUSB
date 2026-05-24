# Copilot instructions — TeslaUSB

Working notes for any Copilot agent (CLI, cloud, code review) operating
on this repository. These rules are binding alongside
`docs/03-CODE-QUALITY-CHARTER.md`.

## UI / website work — Playwright is mandatory

Any time you change code that affects the rendered website (templates,
static JS/CSS, blueprint view code, bootstrap payloads, anything served
on `cybertruckusb.local` or under `web/teslausb_web/`), you **must**
verify the change end-to-end with Playwright before declaring the task
done. "Tests pass" and "the endpoint returns 200" are not sufficient.

For every UI-affecting change:

1. **Drive the actual page with Playwright** (headless Chromium against
   `http://cybertruckusb.local/...` once deployed, or against a local
   Flask dev server). Don't just curl JSON endpoints — confirm the
   browser executes the JS, calls the expected endpoints, and renders.
2. **Capture and assert on perf**: navigation TTFB, DOMContentLoaded,
   first-contentful-paint, and the elapsed time of each network request.
   Surface the slowest 5-10 requests in the report. A page that takes
   more than ~2 s to be interactive on the Pi is not "fast" — keep
   iterating.
3. **Capture and assert on console**: subscribe to `page.on("console")`
   and `page.on("pageerror")`. Any `error`/`warning`/`pageerror` is a
   failure unless explicitly justified in the report.
4. **Visually verify**: take a screenshot at the relevant viewport
   (mobile 375px and desktop ≥1280px, per the UI/UX design system) and
   confirm the change actually appears. Don't trust DOM-only assertions
   when the bug could be CSS, z-index, or layout.
5. **Verify the wiring**: confirm the JS module that you changed is
   actually loaded by the page (a real failure mode in this repo —
   `static/js/mapping.js` was edited for weeks while the page rendered
   from `templates/mapping.html` which never loaded it). Inspect
   `window.<bootstrap-var>`, the network waterfall, and the `<script>`
   tags in the served HTML to prove the right code is running.
6. **Report what changed and what didn't**: include before/after
   timings, the network-request table, console log, and a screenshot
   path in the task summary. If perf regressed, fix it before
   declaring done.

A reusable probe lives at
`~/.copilot/session-state/<session>/files/perf_probe.py` (and prior
checkpoints) — adapt rather than rewriting from scratch.

## Charter still binds

This file does not override `docs/03-CODE-QUALITY-CHARTER.md`. The
charter wins on any conflict; this file just makes the Playwright
verification step non-optional for UI work.
