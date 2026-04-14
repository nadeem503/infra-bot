"""Device lifecycle GitHub Actions — dispose and device-host (org migration).

Two actions, both trigger workflow_dispatch on LambdatestIncPrivate/migrations:

  DeviceDisposeAction   → realdevice-db-lmds-dispose-update.yml
    Marks devices as disposed/inactive.
    Also triggers realdevice-remove-binaries to clean up device binaries.
    Inputs: host_ip,udid pairs · jira · environment · status · remark

  DeviceHostUpdateAction → realdevice-db-lmds-device-host.yml
    Updates device_host record for org assignment / migration / status change.
    Handles deallocation from old orgs, cache invalidation, downtime window setup.
    Inputs: udids · host_ips · jira · environment · status · dedicated_org · cleanup · remark

Why trigger GitHub Actions instead of direct SQL?
  The migrations workflow has: DB secrets, environment protection gates (prod requires
  manual approval), row-limit guards (aborts if >20 rows affected), audit trail,
  and deallocation API calls already wired in. The bot re-uses all of this safely.
"""
from __future__ import annotations

import datetime
import json

from bot.actions.github_workflow_action import GitHubWorkflowAction
from utils.logger import get_logger

logger = get_logger(__name__)

# ── Dispose workflow ────────────────────────────────────────────────────────

# Exact remark option strings accepted by the workflow's choice input
_VALID_DISPOSE_REMARKS: frozenset[str] = frozenset({
    "Device battery bloated",
    "Device screen is not working",
    "Device needs to be repaired",
    "Device is deprecated",
    "others",
})

# Aliases → canonical remark strings so Claude/users can say "bloated" etc.
_REMARK_ALIASES: dict[str, str] = {
    "battery bloated":        "Device battery bloated",
    "bloated battery":        "Device battery bloated",
    "bloated":                "Device battery bloated",
    "screen not working":     "Device screen is not working",
    "screen broken":          "Device screen is not working",
    "broken screen":          "Device screen is not working",
    "screen issue":           "Device screen is not working",
    "needs repair":           "Device needs to be repaired",
    "repair":                 "Device needs to be repaired",
    "deprecated":             "Device is deprecated",
    "end of life":            "Device is deprecated",
    "eol":                    "Device is deprecated",
}


def _to_str(v) -> str:
    """Safely coerce a param value to a stripped string.

    Claude sometimes returns a list instead of a space-separated string
    (e.g. udids=["UDID1","UDID2"] instead of "UDID1 UDID2").
    Joining with a space keeps the existing split-on-whitespace logic intact.
    """
    if v is None:
        return ""
    if isinstance(v, list):
        return " ".join(str(x) for x in v).strip()
    return str(v).strip()


def _normalize_remark(remark: str) -> str:
    """Map user-supplied remark to a valid workflow choice option."""
    if not remark:
        return "others"
    if remark in _VALID_DISPOSE_REMARKS:
        return remark
    return _REMARK_ALIASES.get(remark.lower().strip(), "others")


def _workflow_ref(env: str) -> str:
    """Map environment name to the correct git ref for workflow_dispatch."""
    return "stage" if env.lower() == "stage" else "main"


# ── DeviceDisposeAction ─────────────────────────────────────────────────────

class DeviceDisposeAction(GitHubWorkflowAction):
    """Mark one or more devices as disposed or inactive.

    Triggers: realdevice-db-lmds-dispose-update.yml on LambdatestIncPrivate/migrations

    What the workflow does:
      1. Parses host_ip,udid pairs → builds WHERE udid IN (...)
      2. UPDATE device_host SET status=?, remark=?, dedicated_org=NULL, cleanup="full"
      3. Deallocates from any existing dedicated orgs via mobile API
      4. Clears Redis cache for those orgs
      5. Triggers realdevice-remove-binaries Jenkins job to clean binaries

    Required params (in self.params):
      host_udid_pairs  space-separated "host_ip,udid" e.g. "10.151.1.1,UDID1 10.151.1.2,UDID2"
      jira             Jira ticket ID e.g. TTN-12345
    Optional params:
      environment      stage | prod  (default: stage)
      status           disposed | inactive  (default: disposed)
      remark           remark reason (default: others)
      where_status     status filter  (default: "active faulty maintenance")
    """

    action_type  = "device_dispose"
    workflow_file = "realdevice-db-lmds-dispose-update.yml"

    def dry_run(self) -> str:
        pairs       = (self.params.get("host_udid_pairs") or "").strip()
        jira        = self.params.get("jira") or "TTN-?"
        env         = (self.params.get("environment") or "stage").lower()
        status      = self.params.get("status") or "disposed"
        remark      = _normalize_remark(self.params.get("remark") or "")
        where_status = self.params.get("where_status") or "active faulty maintenance"
        today       = datetime.date.today().strftime("%Y-%m-%d")
        final_remark = f"{jira}-{remark}-{today}" if remark != "others" else f"{jira}-{today}"

        # Parse pairs for SQL preview
        udids = [p.split(",")[1] if "," in p else p for p in pairs.split() if p]
        udid_sql  = ", ".join(f"'{u}'" for u in udids) or "?"
        status_sql = ", ".join(f"'{s}'" for s in where_status.split())

        device_count = len(udids)
        return (
            f"*Dry-run: Mark {device_count} device(s) as `{status}` on `{env}`*\n"
            f"• Jira: `{jira}`\n"
            f"• Pairs: `{pairs}`\n"
            f"• Remark: `{final_remark}`\n"
            f"• Workflow: `{self.workflow_file}`\n\n"
            f"*Resulting SQL (generated by workflow):*\n"
            f"```UPDATE lambda_lmds.device_host\n"
            f"SET status=\"{status}\",\n"
            f"    remark=\"{final_remark}\",\n"
            f"    dedicated_org=NULL,\n"
            f"    cleanup=\"full\"\n"
            f"WHERE udid IN ({udid_sql})\n"
            f"  AND status IN ({status_sql});```\n"
            f"*Post-update:* `realdevice-remove-binaries` Jenkins job auto-triggered"
        )

    def execute(self) -> dict:
        pairs        = _to_str(self.params.get("host_udid_pairs"))
        jira         = _to_str(self.params.get("jira"))
        env          = (_to_str(self.params.get("environment")) or "stage").lower()
        status       = _to_str(self.params.get("status")) or "disposed"
        remark       = _normalize_remark(_to_str(self.params.get("remark")))
        where_status = _to_str(self.params.get("where_status")) or "active faulty maintenance"

        if not pairs:
            return {
                "success": False,
                "message": ":warning: No `host_ip,udid` pairs provided. Format: `10.x.x.x,UDID1 10.x.x.x,UDID2`",
                "details": {},
            }
        if not jira or jira in ("TTN-", "TTN"):
            return {
                "success": False,
                "message": ":warning: Jira ticket ID is required (e.g. `TTN-12345`)",
                "details": {},
            }
        if status not in ("disposed", "inactive"):
            return {
                "success": False,
                "message": f":warning: Invalid status `{status}` — must be `disposed` or `inactive`",
                "details": {},
            }

        inputs = {
            "environment":          env,
            "jira":                 jira,
            "where_host_ip_udids":  pairs,
            "where_status":         where_status,
            "status":               status,
            "remark":               remark,
        }

        result = self._trigger_workflow(inputs, ref=_workflow_ref(env))
        if result["success"]:
            udid_count = len([p for p in pairs.split() if p])
            runs_url   = result.get("runs_url", "")
            result["message"] = (
                f":white_check_mark: Dispose workflow triggered — "
                f"`{udid_count}` device(s) → `{status}` on `{env}`\n"
                + (f"<{runs_url}|View workflow run>" if runs_url else "")
            )
        return result


# ── DeviceHostUpdateAction ──────────────────────────────────────────────────

class DeviceHostUpdateAction(GitHubWorkflowAction):
    """Update device_host record — org assignment, migration, or status change.

    Triggers: realdevice-db-lmds-device-host.yml on LambdatestIncPrivate/migrations

    What the workflow does:
      1. Builds WHERE clause from udids / host_ips / where_status
      2. UPDATE device_host SET status, dedicated_org, cleanup, manual, automation,
         features, remark  (only non-empty fields)
      3. If dedicated_org is changing:
         - Deallocates devices from old orgs via mobile API
         - Deletes reservations per UDID per org
         - Clears Redis private-cloud-devices cache for old and new org
      4. If dedicated_org is set (non-NULL): inserts downtime_window for new org
         if it doesn't already exist

    Required params (at least one of udids / host_ips):
      udids        space-separated UDIDs  (where_UDIDS in workflow)
      host_ips     space-separated IPs    (where_host_ip in workflow)
      jira         Jira ticket ID
    Optional params:
      environment  stage | prod  (default: stage)
      status       active | maintenance | faulty | disposed | inactive
      dedicated_org  org ID integer, or "NULL" to move to public cloud
      cleanup      full | dedicated | adaptive
      remark       free text
      where_status  status filter  (default: "active faulty maintenance")
      manual / automation / features  (additional_args fields)
    """

    action_type   = "device_migrate"
    workflow_file = "realdevice-db-lmds-device-host.yml"

    def dry_run(self) -> str:
        udids        = (self.params.get("udids") or "").strip()
        host_ips     = (self.params.get("host_ips") or "").strip()
        jira         = self.params.get("jira") or "TTN-?"
        env          = (self.params.get("environment") or "stage").lower()
        status       = self.params.get("status") or ""
        dedicated_org = self.params.get("dedicated_org") or ""
        cleanup      = self.params.get("cleanup") or ""
        remark       = self.params.get("remark") or ""
        where_status = self.params.get("where_status") or "active faulty maintenance"

        # Build preview SET clause
        set_parts = []
        if status:
            set_parts.append(f'status="{status}"')
        if dedicated_org:
            val = "NULL" if dedicated_org == "NULL" else f'"{dedicated_org}"'
            set_parts.append(f"dedicated_org={val}")
            if dedicated_org == "NULL":
                set_parts.append("device_custom_name=NULL")
        if cleanup:
            set_parts.append(f'cleanup="{cleanup}"')
        if remark:
            set_parts.append(f'remark="{remark}"')
        set_clause = ", ".join(set_parts) or "<no field changes>"

        # Build preview WHERE clause
        where_parts = []
        if udids:
            where_parts.append(f"udid IN ({', '.join(repr(u) for u in udids.split())})")
        if host_ips:
            where_parts.append(f"host_ip IN ({', '.join(repr(ip) for ip in host_ips.split())})")
        if where_status:
            where_parts.append(f"status IN ({', '.join(repr(s) for s in where_status.split())})")
        where_clause = " AND ".join(where_parts) or "<missing WHERE>"

        # Org notes
        extra_notes = []
        if dedicated_org and dedicated_org != "NULL":
            extra_notes.append(
                f"• :link: Org `{dedicated_org}` — will insert downtime window if new org\n"
                f"• :x: Deallocation from previous org(s) + reservation deletion"
            )
        elif dedicated_org == "NULL":
            extra_notes.append("• :globe_with_meridians: Moving to *public cloud* (dedicated_org=NULL)")

        notes_str = ("\n" + "\n".join(extra_notes)) if extra_notes else ""

        return (
            f"*Dry-run: Device host update on `{env}`*\n"
            f"• Jira: `{jira}`\n"
            f"• UDIDs: `{udids or '(from host_ip)'}`\n"
            f"• Host IPs: `{host_ips or '(from udids)'}`\n"
            f"• Workflow: `{self.workflow_file}`{notes_str}\n\n"
            f"*Resulting SQL (generated by workflow):*\n"
            f"```UPDATE lambda_lmds.device_host\n"
            f"SET {set_clause}\n"
            f"WHERE {where_clause};```"
        )

    def execute(self) -> dict:
        udids        = _to_str(self.params.get("udids"))
        host_ips     = _to_str(self.params.get("host_ips"))
        jira         = _to_str(self.params.get("jira"))
        env          = (_to_str(self.params.get("environment")) or "stage").lower()
        status       = _to_str(self.params.get("status"))
        dedicated_org = _to_str(self.params.get("dedicated_org"))
        cleanup      = _to_str(self.params.get("cleanup"))
        remark       = _to_str(self.params.get("remark"))
        where_status = _to_str(self.params.get("where_status")) or "active faulty maintenance"
        manual       = _to_str(self.params.get("manual"))
        automation   = _to_str(self.params.get("automation"))
        features     = _to_str(self.params.get("features"))

        if not udids and not host_ips:
            return {
                "success": False,
                "message": ":warning: At least one UDID or host IP is required for a WHERE clause",
                "details": {},
            }
        if not jira or jira in ("TTN-", "TTN"):
            return {
                "success": False,
                "message": ":warning: Jira ticket ID is required (e.g. `TTN-12345`)",
                "details": {},
            }

        additional_args = json.dumps({
            "manual": manual,
            "automation": automation,
            "features": features,
        })

        # Build inputs dict — skip empty strings (GitHub Actions ignores
        # missing optional inputs, but passing "" can cause validation errors)
        inputs: dict[str, str] = {}
        inputs["environment"]      = env
        inputs["jira"]             = jira
        inputs["additional_args"]  = additional_args
        if udids:
            inputs["where_UDIDS"]  = udids
        if host_ips:
            inputs["where_host_ip"] = host_ips
        if where_status:
            inputs["where_status"] = where_status
        if status:
            inputs["status"]       = status
        if dedicated_org:
            inputs["dedicated_org"] = dedicated_org
        if cleanup:
            inputs["cleanup"]      = cleanup
        if remark:
            inputs["remark"]       = remark

        result = self._trigger_workflow(inputs, ref=_workflow_ref(env))
        if result["success"]:
            runs_url = result.get("runs_url", "")
            target   = udids or host_ips

            changes = []
            if status:
                changes.append(f"status → `{status}`")
            if dedicated_org:
                org_label = "public cloud" if dedicated_org == "NULL" else f"org `{dedicated_org}`"
                changes.append(f"assigned to {org_label}")
            if cleanup:
                changes.append(f"cleanup → `{cleanup}`")
            changes_str = ", ".join(changes) if changes else "field update"

            result["message"] = (
                f":white_check_mark: Device migration workflow triggered on `{env}` — "
                f"`{target}` | {changes_str}\n"
                + (f"<{runs_url}|View workflow run>" if runs_url else "")
            )
        return result
