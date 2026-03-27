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
```
Then: `Read tmp/dq.png`

The `--auth` flag fetches admin credentials from AWS Secrets Manager (`judgemind/dev/agent-admin`), logs in via the web login form, and then navigates to the target page. This is required for any page behind authentication (e.g. `/admin/*` routes).

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--output`, `-o` | `tmp/screenshot.png` | Output file path |
| `--full-page` | off | Capture full scrollable page |
| `--selector`, `-s` | none | CSS selector to screenshot a specific element |
| `--width` | 1280 | Viewport width in pixels |
| `--height` | 720 | Viewport height in pixels |
| `--wait` | 3000 | Wait time in ms after page load for JS rendering |
| `--auth` | off | Log in as admin before taking the screenshot. Fetches credentials from AWS Secrets Manager (`judgemind/dev/agent-admin`). Required for admin pages. |

---

## Workflow pattern

1. Take a screenshot to see the current state
2. Analyze what needs to change
3. Edit the code
4. Take another screenshot to verify the fix
5. Repeat until it looks right

Always save screenshots to `{worktree}/tmp/` (or `tmp/` from the repo root). This directory is gitignored.