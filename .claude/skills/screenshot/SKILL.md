---
description: Take a screenshot of a page on dev.judgemind.org for visual iteration. Use when working on frontend tasks to see what the page looks like, verify layout changes, or debug UI issues.
argument-hint: "/rulings"
---

# /screenshot skill

Capture a screenshot of a page on `dev.judgemind.org` and display it. This lets you visually inspect pages while iterating on frontend code.

**When to use:** During frontend development — to see the current state of a page, verify a fix, check layout, or debug a UI issue.

**Restriction:** Only `dev.judgemind.org` URLs are allowed. The script rejects any other host.

**No setup required.** The script auto-bootstraps its own venv with playwright and chromium on first run. The venv lives at `<repo-root>/.venv/` and is reused across sessions and worktrees.

---

## Usage

Run the screenshot script from the repo root (or worktree root):

```
scripts/run-py.sh scripts/screenshot.py <path> [options]
```

The script saves the screenshot and prints the absolute path. Then use the **Read tool** to view the image — Claude Code can read and analyze PNG images natively.

### Examples

**Screenshot the rulings page:**
```
scripts/run-py.sh scripts/screenshot.py /rulings --output tmp/rulings.png
```
Then: `Read tmp/rulings.png`

**Full-page screenshot (captures below the fold too):**
```
scripts/run-py.sh scripts/screenshot.py /rulings --full-page --output tmp/rulings-full.png
```

**Screenshot a specific element:**
```
scripts/run-py.sh scripts/screenshot.py /rulings --selector ".ruling-card" --output tmp/card.png
```

**Custom viewport (e.g. mobile):**
```
scripts/run-py.sh scripts/screenshot.py /rulings --width 375 --height 812 --output tmp/mobile.png
```

**Longer wait for slow pages:**
```
scripts/run-py.sh scripts/screenshot.py /rulings --wait 5000 --output tmp/rulings.png
```

**Screenshot an admin page (requires authentication):**
```
scripts/run-py.sh scripts/screenshot.py --auth /admin/data-quality --output tmp/dq.png
scripts/run-py.sh scripts/screenshot.py --auth /admin/dispatcher --output tmp/dispatcher.png
```
Then: `Read tmp/dq.png`

The `--auth` flag fetches admin credentials from AWS Secrets Manager (`judgemind/dev/agent-admin`), logs in via the web login form, and then navigates to the target page. This is required for any page behind authentication (e.g. `/admin/*` routes).

The `agent-admin` account on dev has `users.role = 'admin'`, so both admin dashboards (`/admin/data-quality`, `/admin/dispatcher`) render with full admin content. To add a new admin-gated page, gate on `user.role === 'admin'` and `--auth` will Just Work.

**Capture state behind a single click (modal, dropdown, popover):**
```
scripts/run-py.sh scripts/screenshot.py --auth /admin/dispatcher \
    --click '[data-testid="queue-ready-count"]' \
    --output tmp/ready_dialog.png
```

The `--click <selector>` flag performs one `page.click(selector)` after the existing `--wait` and before the screenshot, so the captured image shows the post-click state. Pair with `--click-wait <ms>` (default 500) so animated UIs (Radix Dialog, etc.) finish rendering before the snapshot — set `--click-wait 0` to capture a mid-animation frame, or bump it higher for slow transitions.

If the selector matches nothing, the script exits non-zero with `Click target not found: <selector>` rather than letting Playwright time out. Multi-step flows (open menu, then click item, then type) are out of scope — write a one-off Playwright script for those.

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--output`, `-o` | `tmp/screenshot.png` | Output file path |
| `--full-page` | off | Capture full scrollable page |
| `--selector`, `-s` | none | CSS selector to screenshot a specific element |
| `--width` | 1280 | Viewport width in pixels |
| `--height` | 720 | Viewport height in pixels |
| `--wait` | 3000 | Wait time in ms after page load for JS rendering |
| `--auth` | off | Log in as admin before taking the screenshot. Fetches credentials from AWS Secrets Manager (`judgemind/dev/agent-admin`, a `role='admin'` user on dev). Required for admin pages. |
| `--click` | none | CSS selector to click after the `--wait` and before the screenshot. Use to capture modals/dropdowns/popovers behind a single interaction. Exits non-zero if the selector matches nothing. |
| `--click-wait` | 500 | Wait time in ms after `--click` for animations (Radix Dialog ~200ms etc.) to settle before the screenshot. |

---

## Workflow pattern

1. Take a screenshot to see the current state
2. Analyze what needs to change
3. Edit the code
4. Take another screenshot to verify the fix
5. Repeat until it looks right

Always save screenshots to `{worktree}/tmp/` (or `tmp/` from the repo root). This directory is gitignored.
