"""Slack Block Kit message formatter for infra-bot responses.

Supports multiple DC owners per region.
Auto-detects mention format:
  U... -> <@USERID>   (user mention)
  C... -> <#CHANNELID> (channel mention)
"""
from __future__ import annotations

import time
from typing import Optional

from utils.config_loader import get_dc_owners
from utils.logger import get_logger

logger = get_logger(__name__)

# Human-readable card headers keyed by issue_category.
# Replaces the hardcoded "Infra AI Response" so approvers immediately know what they're looking at.
_ISSUE_LABELS: dict[str, str] = {
    "lrr_down":               ":apple: LRR Restart Requested",
    "resigner_down":          ":key: Resigner Restart Requested",
    "ihm_down":               ":gear: IHM Restart Requested",
    "reconciler_down":        ":arrows_counterclockwise: Reconciler Restart Requested",
    "lrp_down":               ":electric_plug: LRP Restart Requested",
    "cert_expired":           ":closed_lock_with_key: Certificate Expired — Resigner Needed",
    "device_down":            ":iphone: Device Issue Detected",
    "reboot":                 ":recycle: Device Reboot Requested",
    "adb_issue":              ":android: ADB Issue Detected",
    "network_issue":          ":globe_with_meridians: Network Issue Detected",
    "device_disconnected":    ":electric_plug: Device Disconnected",
    "rmdm_down":              ":gear: RMDM Service Down",
    "rdtsa_down":             ":gear: RDTSA Service Down",
    "android_container_down": ":whale: Android Container Down",
    "host_service_status":    ":mag: Host Service Status Check",
    "storage_issue":          ":floppy_disk: Storage Issue Detected",
    "app_crash":              ":bug: App Crash Detected",
    "jenkins_failure":        ":construction: Jenkins Job Failed",
    "db_mismatch":            ":bar_chart: DB Mismatch Detected",
    "db_query":               ":mag: Database Query Result",
    "faulty_devices_report":  ":warning: Faulty Customer Dedicated Devices",
    "device_dispose":         ":coffin: Device Dispose Request",
    "device_migrate":         ":truck: Device Migration / Org Assignment",
}


def _make_mention(slack_id: str) -> str:
    if slack_id.startswith("C"):
        return f"<#{slack_id}>"
    return f"<@{slack_id}>"


class SlackFormatter:
    def __init__(self) -> None:
        self._owners: Optional[dict] = None

    @property
    def owners(self) -> dict:
        if self._owners is None:
            self._owners = get_dc_owners()
        return self._owners

    def get_owner(self, region: Optional[str]) -> dict:
        return self.owners.get(region or "") or self.owners.get("default", {})

    def get_owner_mentions(self, region: Optional[str]) -> tuple[str, str]:
        owner = self.get_owner(region)
        name = owner.get("name", "Unknown")
        ids: list[str] = owner.get("slack_ids") or []
        if not ids and owner.get("slack_id"):
            ids = [owner["slack_id"]]
        mention = " ".join(_make_mention(sid) for sid in ids) if ids else name
        return mention, name

    def format_analysis(
        self,
        issue_type: Optional[str],
        region: Optional[str],
        region_display: str,
        devices: list[str],
        proposed_actions: list[str],
        action_records: list[dict],
        personality_warnings: list[str] | None = None,
        recommendation: dict | None = None,
        prior_patterns: list[dict] | None = None,
    ) -> list[dict]:
        """Build Block Kit blocks for the main analysis response."""
        owner_mention, owner_name = self.get_owner_mentions(region)
        device_list = ", ".join(f"`{d}`" for d in devices) if devices else "_None identified_"
        actions_text = (
            "\n".join(f"\u2022 {a}" for a in proposed_actions)
            if proposed_actions
            else "\u2022 Device status check"
        )

        # Fix #9: dynamic header — approvers see "LRR Restart Requested" not "Infra AI Response",
        # so they know immediately what they're approving without reading the detail lines.
        header = _ISSUE_LABELS.get(issue_type or "", ":rotating_light: Infra Issue Detected")
        main_text = (
            f"*{header}*\n"
            f"\u2022 *Issue Detected:* `{issue_type or 'general'}`\n"
            f"\u2022 *Region:* {region_display}\n"
            f"\u2022 *Devices:* {device_list}\n"
            f"\u2022 *DC Owners:* {owner_mention} ({owner_name})\n"
            f"\u2022 *Action Plan:*\n{actions_text}\n"
            "\u2022 *Executing:* Awaiting approval :hourglass_flowing_sand:"
        )

        # Fix #10: timestamp + action ID in a context block so approvers can see card age
        # at a glance. Slack renders <!date^...> as a local time for every viewer's timezone.
        action_id_hint = action_records[0]["action_id"] if action_records else ""
        ts_val = int(time.time())
        blocks: list[dict] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": main_text}},
            {
                "type": "context",
                "elements": [{
                    "type": "mrkdwn",
                    "text": (
                        f":clock1: Requested <!date^{ts_val}^{{time_secs}}|just now>"
                        + (f"  \u00b7  Action ID: `{action_id_hint}`" if action_id_hint else "")
                    ),
                }],
            },
            {"type": "divider"},
        ]

        # Learning recommendation
        if recommendation:
            pct = int(recommendation["success_rate"] * 100)
            blocks.append({
                "type": "context",
                "elements": [{
                    "type": "mrkdwn",
                    "text": (
                        f":brain: *Learned:* `{recommendation['action_type']}` fixed this "
                        f"{pct}% of the time ({recommendation['success']}/"
                        f"{recommendation['total']} runs in `{region or 'unknown'}`)"
                    ),
                }],
            })

        # Operator-noted fix patterns — shown when a previous operator told the bot
        # "note the pattern" for this issue_type. Gives the approver instant context:
        # "someone fixed this before by doing X, here's what they noted."
        for p in (prior_patterns or [])[:2]:
            pattern_text = p.get("pattern", "")
            steps: list[str] = p.get("steps") or []
            steps_chain = " \u2192 ".join(f"`{s}`" for s in steps[:4]) if steps else ""
            preview_parts = []
            if pattern_text:
                preview_parts.append(pattern_text[:120])
            if steps_chain:
                preview_parts.append(f"*Steps:* {steps_chain}")
            if preview_parts:
                blocks.append({
                    "type": "context",
                    "elements": [{
                        "type": "mrkdwn",
                        "text": ":memo: *Operator noted:* " + "  \u00b7  ".join(preview_parts),
                    }],
                })

        # Device personality warnings
        for w in (personality_warnings or []):
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": w}})

        for record in action_records:
            blocks.extend(self._approval_buttons(record))
        return blocks

    def _approval_buttons(self, record: dict) -> list[dict]:
        action_id = record["action_id"]
        action_type = record["action_type"]
        dry_run_preview = record.get("dry_run_preview")

        blocks: list[dict] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": f":wrench: *Proposed Action:* `{action_type}`"}},
        ]

        if dry_run_preview:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f":mag: *Dry Run Preview:*\n```{dry_run_preview}```"},
            })

        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "\u2705 Approve"},
                    "style": "primary",
                    "action_id": "approve_action",
                    "value": action_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "\U0001f680 Execute Now"},
                    "style": "primary",
                    "action_id": "execute_now_action",
                    "value": action_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "\u274c Deny"},
                    "style": "danger",
                    "action_id": "deny_action",
                    "value": action_id,
                },
            ],
        })
        return blocks

    def format_clarification_card(self, clarify_id: str, options: list[dict]) -> list[dict]:
        """Block Kit card with A/B/C buttons for low-confidence messages."""
        elements = []
        labels = ["A", "B", "C"]
        for i, opt in enumerate(options[:3]):
            elements.append({
                "type": "button",
                "text": {"type": "plain_text", "text": f"{labels[i]}) {opt.get('label', f'Option {labels[i]}')[:40]}"},
                "action_id": "clarify_choice",
                "value": f"{clarify_id}:{i}",
            })
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": ":thinking_face: *Not sure what you mean — did you want me to:*",
                },
            },
            {"type": "actions", "elements": elements},
        ]

    # Fix #11: status icons for /infra status output.
    # pre_approved means step 1 of a double-approval flow is done — a second approver is still needed.
    # Without the status column, all items looked identical and the "1/2 approved" state was invisible.
    _STATUS_ICONS: dict[str, str] = {
        "pending":      ":hourglass:",
        "pre_approved": ":ballot_box_with_check: 1/2",
    }

    def format_pending_list(self, records: list) -> str:
        if not records:
            return ":white_check_mark: No pending actions right now."
        lines = [f":hourglass: *{len(records)} awaiting approval:*\n"]
        for rec in records:
            age_min = max(0, int((time.time() - rec.requested_at) / 60))
            devices_str = ", ".join(f"`{d}`" for d in rec.devices[:3])
            if len(rec.devices) > 3:
                devices_str += f" +{len(rec.devices) - 3} more"
            if not devices_str:
                devices_str = "_no device_"
            status_icon = self._STATUS_ICONS.get(rec.status, ":hourglass:")
            lines.append(
                f"\u2022 `{rec.action_id}` \u2014 `{rec.action_type}` | "
                f"{rec.region} | {devices_str} | {age_min}m ago | "
                f"<@{rec.requested_by}> | {status_icon} `{rec.status}`"
            )
        return "\n".join(lines)

    def format_result(self, action_type: str, result: dict) -> str:
        icon = ":white_check_mark:" if result.get("success") else ":x:"
        message = result.get("message", "No message")
        details = result.get("details", {})
        text = f"{icon} *Action Completed:* `{action_type}`\n{message}"

        # Fix #8: render per-device breakdown when present.
        # LRRRestartAction returns results=[{udid, loaded, ios_version, note, load_rc}].
        # Without this, approvers only saw "LRR reloaded 2/3 devices" — no way to know
        # which UDID failed or was skipped due to iOS < 12.4.
        per_device = result.get("results") or []
        if per_device:
            for r in per_device:
                ok = r.get("loaded") or r.get("success")
                # Skipped (iOS < 12.4) gets a different icon from a hard failure
                d_icon = (
                    ":arrow_right_hook:" if r.get("note") and not ok
                    else ":white_check_mark:" if ok
                    else ":x:"
                )
                udid = r.get("udid", "")
                short_id = (udid[:8] + "\u2026") if len(udid) > 8 else udid
                note = r.get("note") or ""
                if not note and r.get("ios_version"):
                    note = f"iOS {r['ios_version']}"
                elif not note and r.get("load_rc") is not None:
                    note = f"rc={r['load_rc']}"
                text += f"\n  {d_icon} `{short_id}` {('— ' + note) if note else ''}"

        if details.get("output"):
            output = details["output"]
            truncated = len(output) > 400
            # Fix #7: append a truncation notice so operators know there is more output
            # to look at in logs — previously the cut-off was silent.
            text += f"\n```{output[:400]}```"
            if truncated:
                text += f"\n_\u2026 output truncated ({len(output):,} chars total — check bot logs for full output)_"
        if details.get("rows"):
            text += f"\n_Returned {len(details['rows'])} row(s)_"
        if details.get("url"):
            text += f"\n<{details['url']}|View ticket>"
        return text

    def format_db_result(self, result: dict) -> str:
        """Format DB results.

        Single-device lookup (1 row with udid) → vertical key-value card.
        Breakdown / multi-row → box-drawing ASCII table in code block.
        """
        details = result.get("details", {})
        rows: list[dict] = details.get("rows") or []

        if not result.get("success"):
            return self.format_error(result.get("message", "DB query failed"))

        if not rows:
            return ":mag: *DB Query* — no rows matched."

        _STATUS_ICONS = {
            "active":      ":large_green_circle:",
            "busy":        ":large_blue_circle:",
            "faulty":      ":red_circle:",
            "cleanup":     ":large_yellow_circle:",
            "maintenance": ":large_yellow_circle:",
            "inactive":    ":white_circle:",
            "disposed":    ":black_circle:",
        }

        # ── Single device lookup → vertical card ─────────────────────────────
        if len(rows) == 1 and "udid" in rows[0]:
            row    = rows[0]
            status = str(row.get("status", "")).lower()
            icon   = _STATUS_ICONS.get(status, ":grey_question:")
            remark = str(row.get("remark") or "—")
            if len(remark) > 60:
                remark = remark[:57] + "..."
            org    = str(row.get("dedicated_org") or "—")
            lines  = [
                f"{icon} *Device — {status or '—'}*",
                f"• *UDID:* {row.get('udid', '—')}",
                f"• *Host:* {row.get('host_ip', '—')}",
                f"• *Org:* {org}  •  *Cleanup:* {row.get('cleanup', '—')}  •  *Region:* {row.get('region', '—')}",
                f"• *Remark:* {remark}",
            ]
            return "\n".join(lines)

        # ── Breakdown / multi-row → code block table ──────────────────────────
        display_rows = rows[:20]

        col_order = ["host_ip", "udid", "status", "dedicated_org", "cleanup", "remark"]
        cols = [c for c in col_order if c in display_rows[0]]
        for c in display_rows[0]:
            if c not in cols:
                cols.append(c)

        def _cell(row: dict, col: str) -> str:
            v = row.get(col)
            if v is None or v == "":
                return "-"
            s = str(v)
            if col == "remark" and len(s) > 40:
                s = s[:37] + "..."
            return s

        widths = {c: len(c) for c in cols}
        for row in display_rows:
            for c in cols:
                widths[c] = max(widths[c], len(_cell(row, c)))

        def _fmt_row(cells: list[str]) -> str:
            return "│ " + " │ ".join(v.ljust(widths[c]) for v, c in zip(cells, cols)) + " │"

        def _border(l: str, m: str, r: str) -> str:
            return l + m.join("─" * (widths[c] + 2) for c in cols) + r

        table_lines = [
            _border("┌", "┬", "┐"),
            _fmt_row(cols),
            _border("├", "┼", "┤"),
        ]
        for row in display_rows:
            table_lines.append(_fmt_row([_cell(row, c) for c in cols]))
        table_lines.append(_border("└", "┴", "┘"))

        suffix = f"\n_{len(rows) - 20} more row(s) not shown_" if len(rows) > 20 else ""
        return f":mag: *DB — {len(rows)} row(s)*\n```\n" + "\n".join(table_lines) + "\n```" + suffix

    def format_denied(self, action_type: str, denier_id: str) -> str:
        return (
            f":no_entry: Action `{action_type}` was denied by <@{denier_id}>.\n"
            "_If the parameters were wrong, just send your request again with corrections._"
        )

    def format_expired(self, action_type: str) -> str:
        return f":timer_clock: Action `{action_type}` expired (30-minute TTL exceeded)"

    def format_unauthorized(self, user_id: str) -> str:
        return f":lock: <@{user_id}> is not authorized to approve actions"

    def format_error(self, message: str) -> str:
        return f":warning: *Error:* {message}"
