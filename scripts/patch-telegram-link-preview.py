#!/usr/bin/env python3
"""Patch the cached Telegram plugin to add disable_web_page_preview support.

Modifies ~/.claude/plugins/cache/claude-plugins-official/telegram/<version>/server.ts
to add the disable_web_page_preview parameter to both the reply and edit_message
tools in the MCP Telegram plugin.

This is a temporary patch -- an upstream PR has been filed on
anthropics/claude-plugins-official. When the plugin updates to include the
parameter natively, this patch is no longer needed.

Usage:
    python3 scripts/patch-telegram-link-preview.py [--dry-run]
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


def find_server_ts() -> Path:
    """Locate the cached Telegram plugin server.ts."""
    home = Path.home()
    cache_dir = home / ".claude" / "plugins" / "cache" / "claude-plugins-official" / "telegram"
    if not cache_dir.exists():
        print(f"ERROR: Plugin cache directory not found: {cache_dir}", file=sys.stderr)
        sys.exit(1)

    versions = sorted(
        [d for d in cache_dir.iterdir() if d.is_dir()],
        key=lambda d: d.name,
        reverse=True,
    )
    if not versions:
        print(f"ERROR: No version directories found in {cache_dir}", file=sys.stderr)
        sys.exit(1)

    server_ts = versions[0] / "server.ts"
    if not server_ts.exists():
        print(f"ERROR: server.ts not found at {server_ts}", file=sys.stderr)
        sys.exit(1)

    return server_ts


# --- Patch 1: Add disable_web_page_preview to the reply tool schema ---
REPLY_SCHEMA_OLD = """\
          format: {
            type: 'string',
            enum: ['text', 'markdownv2'],
            description: "Rendering mode. 'markdownv2' enables Telegram formatting (bold, italic, code, links). Caller must escape special chars per MarkdownV2 rules. Default: 'text' (plain, no escaping needed).",
          },
        },
        required: ['chat_id', 'text'],
      },
    },
    {
      name: 'react',"""

REPLY_SCHEMA_NEW = """\
          format: {
            type: 'string',
            enum: ['text', 'markdownv2'],
            description: "Rendering mode. 'markdownv2' enables Telegram formatting (bold, italic, code, links). Caller must escape special chars per MarkdownV2 rules. Default: 'text' (plain, no escaping needed).",
          },
          disable_web_page_preview: {
            type: 'boolean',
            description: "Suppress link preview cards. When true, URLs in the message text will not generate a preview snippet. Default: false.",
          },
        },
        required: ['chat_id', 'text'],
      },
    },
    {
      name: 'react',"""


# --- Patch 2: Extract the parameter and pass it to sendMessage ---
REPLY_HANDLER_OLD = """\
        const format = (args.format as string | undefined) ?? 'text'
        const parseMode = format === 'markdownv2' ? 'MarkdownV2' as const : undefined"""

REPLY_HANDLER_NEW = """\
        const format = (args.format as string | undefined) ?? 'text'
        const parseMode = format === 'markdownv2' ? 'MarkdownV2' as const : undefined
        const disablePreview = args.disable_web_page_preview === true"""


SEND_MESSAGE_OLD = """\
            const sent = await bot.api.sendMessage(chat_id, chunks[i], {
              ...(shouldReplyTo ? { reply_parameters: { message_id: reply_to } } : {}),
              ...(parseMode ? { parse_mode: parseMode } : {}),
            })"""

SEND_MESSAGE_NEW = """\
            const sent = await bot.api.sendMessage(chat_id, chunks[i], {
              ...(shouldReplyTo ? { reply_parameters: { message_id: reply_to } } : {}),
              ...(parseMode ? { parse_mode: parseMode } : {}),
              ...(disablePreview ? { link_preview_options: { is_disabled: true } } : {}),
            })"""


# --- Patch 3: Add disable_web_page_preview to the edit_message tool schema ---
EDIT_SCHEMA_OLD = """\
          format: {
            type: 'string',
            enum: ['text', 'markdownv2'],
            description: "Rendering mode. 'markdownv2' enables Telegram formatting (bold, italic, code, links). Caller must escape special chars per MarkdownV2 rules. Default: 'text' (plain, no escaping needed).",
          },
        },
        required: ['chat_id', 'message_id', 'text'],"""

EDIT_SCHEMA_NEW = """\
          format: {
            type: 'string',
            enum: ['text', 'markdownv2'],
            description: "Rendering mode. 'markdownv2' enables Telegram formatting (bold, italic, code, links). Caller must escape special chars per MarkdownV2 rules. Default: 'text' (plain, no escaping needed).",
          },
          disable_web_page_preview: {
            type: 'boolean',
            description: "Suppress link preview cards. When true, URLs in the message text will not generate a preview snippet. Default: false.",
          },
        },
        required: ['chat_id', 'message_id', 'text'],"""


# --- Patch 4: Extract the parameter in edit_message handler and pass to editMessageText ---
EDIT_HANDLER_OLD = """\
      case 'edit_message': {
        assertAllowedChat(args.chat_id as string)
        const editFormat = (args.format as string | undefined) ?? 'text'
        const editParseMode = editFormat === 'markdownv2' ? 'MarkdownV2' as const : undefined
        const edited = await bot.api.editMessageText(
          args.chat_id as string,
          Number(args.message_id),
          args.text as string,
          ...(editParseMode ? [{ parse_mode: editParseMode }] : []),
        )"""

EDIT_HANDLER_NEW = """\
      case 'edit_message': {
        assertAllowedChat(args.chat_id as string)
        const editFormat = (args.format as string | undefined) ?? 'text'
        const editParseMode = editFormat === 'markdownv2' ? 'MarkdownV2' as const : undefined
        const editDisablePreview = args.disable_web_page_preview === true
        const edited = await bot.api.editMessageText(
          args.chat_id as string,
          Number(args.message_id),
          args.text as string,
          {
            ...(editParseMode ? { parse_mode: editParseMode } : {}),
            ...(editDisablePreview ? { link_preview_options: { is_disabled: true } } : {}),
          },
        )"""


PATCHES = [
    ("reply tool schema: add disable_web_page_preview", REPLY_SCHEMA_OLD, REPLY_SCHEMA_NEW),
    ("reply handler: extract disable_web_page_preview", REPLY_HANDLER_OLD, REPLY_HANDLER_NEW),
    ("reply handler: pass link_preview_options to sendMessage", SEND_MESSAGE_OLD, SEND_MESSAGE_NEW),
    ("edit_message tool schema: add disable_web_page_preview", EDIT_SCHEMA_OLD, EDIT_SCHEMA_NEW),
    ("edit_message handler: extract and pass disable_web_page_preview", EDIT_HANDLER_OLD, EDIT_HANDLER_NEW),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Patch Telegram plugin for link preview control")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without modifying files")
    args = parser.parse_args()

    server_ts = find_server_ts()
    print(f"Found plugin at: {server_ts}")

    content = server_ts.read_text()

    if "disable_web_page_preview" in content:
        print("Plugin already patched (disable_web_page_preview found). Nothing to do.")
        sys.exit(0)

    for name, old, new in PATCHES:
        if old not in content:
            print(f"ERROR: Could not find patch target for: {name}", file=sys.stderr)
            print("The plugin version may have changed. Review manually.", file=sys.stderr)
            sys.exit(1)
        content = content.replace(old, new, 1)
        print(f"  [OK] {name}")

    if args.dry_run:
        print("\n--dry-run: no changes written.")
        sys.exit(0)

    backup = server_ts.with_suffix(".ts.bak")
    if not backup.exists():
        shutil.copy2(server_ts, backup)
        print(f"  Backed up original to: {backup}")

    server_ts.write_text(content)
    print(f"\nPatched successfully: {server_ts}")
    print("Restart Claude Code to pick up the changes.")


if __name__ == "__main__":
    main()
