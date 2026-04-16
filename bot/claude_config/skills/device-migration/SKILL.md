---
name: device-migration
description: This skill should be used when the user asks to "migrate a device", "update device host", "move device to dedicated org", "change device status", "assign device to org", "update device_host", mentions "device migration", "device update", "dedicated_org", or discusses updating device fields (status, cleanup, dedicated_org, remark) in the LMDS database.
version: 1.0.0
---

# Device Migration Workflow

This skill triggers the `realdevice-db-lmds-device-host.yml` GitHub Actions workflow on **LambdatestIncPrivate/migrations** to update fields in `lambda_lmds.device_host`.

## When This Skill Applies

This skill activates when the user wants to:
- Migrate a device to a dedicated org or back to public cloud
- Change device status (active, maintenance, faulty, disposed, inactive)
- Update device cleanup mode (full, dedicated, adaptive)
- Update manual/automation/features flags on a device
- Add a remark to a device record
- Perform any update on `lambda_lmds.device_host`

## Workflow Inputs

Collect inputs from the user before triggering. At minimum, one WHERE condition (UDIDs or host IPs) and one SET value must be provided.

### WHERE conditions (at least one required):
1. **where_UDIDS** — Space-separated list of UDIDs (e.g., `00008140-001638801E0B001C 00008120-000A25A63CC2001E`)
2. **where_host_ip** — Space-separated list of host IPs (e.g., `10.151.0.131 10.151.0.132`)
3. **where_status** — Space-separated list of statuses to filter (default: `active faulty maintenance`)

### SET values (at least one required):
4. **status** — New status: `active`, `maintenance`, `faulty`, `disposed`, or `inactive`
5. **dedicated_org** — Org ID to assign, or `NULL` to move back to public cloud
6. **cleanup** — Cleanup mode: `full`, `dedicated`, or `adaptive`
7. **additional_args** — JSON for manual/automation/features fields (default: `{"manual": "", "automation": "", "features": ""}`)
8. **remark** — Remark text to set on the device

### Always required:
9. **environment** — `prod` or `stage` (default: `prod`)
10. **jira** — Jira ticket ID (e.g., `TE-5292`)

### Defaults (do not ask unless user wants to change):
- **where_status**: `active faulty maintenance`
- **additional_args**: `{"manual": "", "automation": "", "features": ""}`

## Execution Steps

1. Parse user input for WHERE conditions and SET values. If neither UDIDs nor host IPs are provided, ask for them.
2. Ask the user for any missing required fields (environment, jira) using AskUserQuestion.
3. Use `--ref` flag matching the environment (`--ref prod` for prod, `--ref stage` for stage).
4. Build and trigger the workflow. Only include `-f` flags for inputs that have values:

```bash
gh workflow run realdevice-db-lmds-device-host.yml \
  --repo LambdatestIncPrivate/migrations \
  --ref <environment> \
  -f environment=<environment> \
  -f jira=<jira_ticket> \
  -f "where_UDIDS=<space_separated_udids>" \
  -f "where_host_ip=<space_separated_host_ips>" \
  -f "where_status=<space_separated_statuses>" \
  -f status=<status> \
  -f dedicated_org=<dedicated_org> \
  -f cleanup=<cleanup> \
  -f 'additional_args={"manual": "", "automation": "", "features": ""}' \
  -f "remark=<remark>"
```

5. On success, fetch the actual workflow run URL:

```bash
gh run list --repo LambdatestIncPrivate/migrations \
  --workflow realdevice-db-lmds-device-host.yml \
  --limit 1
```

Use the run ID to construct the URL: `https://github.com/LambdatestIncPrivate/migrations/actions/runs/<run_id>`

Confirm and share this actual run URL (not the generic workflow URL).

6. **If the user provided a Slack thread URL**, post an approval request in that thread:
   - Parse `channel_id` and `thread_ts` from the URL (format: `/archives/<channel_id>/p<ts_no_dot>?thread_ts=<thread_ts>`)
   - Search for the approver's Slack user ID using `mcp__slack__users_search`
   - Post a reply using `mcp__slack__conversations_add_message` with:
     - List of devices being migrated (IP + UDID)
     - Jira ticket link
     - Actual GitHub Actions run URL
     - `@mention` of the approver requesting approval

   Example message:
   ```
   Raised migration WF for the following devices:

   • `<host_ip>` — <udid>

   Jira: <jira_ticket>
   WF run: https://github.com/LambdatestIncPrivate/migrations/actions/runs/<run_id>

   @<approver> requesting your approval on this.
   ```

## Important Notes

- The workflow has a safety limit of **35 rows** (74 on stage). If more rows are matched, the workflow will abort.
- When changing `dedicated_org`, the workflow automatically:
  - Deallocates devices from old teams via API
  - Deletes reservations for affected UDIDs
  - Clears private cloud device cache for old and new orgs
  - Inserts a downtime window for the new org if one doesn't exist
- Use `dedicated_org=NULL` to move devices back to public cloud.
- Omit any `-f` flag for fields the user did not specify (do not pass empty strings).

## Multiple Devices

Pass multiple UDIDs or host IPs as space-separated values:

```bash
-f "where_UDIDS=udid1 udid2 udid3"
-f "where_host_ip=10.151.0.131 10.151.0.132"
```
