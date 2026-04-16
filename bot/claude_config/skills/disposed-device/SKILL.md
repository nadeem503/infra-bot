---
name: disposed-device
description: This skill should be used when the user asks to "dispose a device", "mark device as disposed", "dispose device", mentions "disposed", "disposal", or discusses marking real devices as disposed or inactive in the LMDS database.
version: 1.0.0
---

# Disposed Device Workflow

This skill triggers the `realdevice-db-lmds-dispose-update.yml` GitHub Actions workflow on **LambdatestIncPrivate/migrations** to mark a real device as disposed/inactive in `lambda_lmds.device_host`.

## When This Skill Applies

This skill activates when the user wants to:
- Mark a device as disposed or inactive
- Run the device disposal workflow
- Remove a device from active/faulty/maintenance status

## Workflow Inputs

The workflow requires the following inputs. Collect them from the user before triggering.

### Required from user:
1. **host_ip** — Device host IP (e.g., `10.151.0.131`)
2. **udid** — Device UDID (e.g., `00008140-001638801E0B001C`)
3. **environment** — `prod` or `stage` (default: `prod`)
4. **jira** — Jira ticket ID (e.g., `TE-5292`)
5. **remark** — Reason for disposal, one of:
   - `Device battery bloated`
   - `Device screen is not working`
   - `Device needs to be repaired`
   - `Device is deprecated`
   - `others`

### Defaults (do not ask unless user wants to change):
- **where_status**: `active faulty maintenance`
- **status**: `disposed`

## Execution Steps

1. Parse user input for `host_ip` and `udid`. If not provided, ask for them.
2. Ask the user for `environment`, `jira` ticket, and `remark` using AskUserQuestion.
3. Use `--ref` flag matching the environment (`--ref prod` for prod, `--ref stage` for stage).
4. Trigger the workflow:

```bash
gh workflow run realdevice-db-lmds-dispose-update.yml \
  --repo LambdatestIncPrivate/migrations \
  --ref <environment> \
  -f environment=<environment> \
  -f jira=<jira_ticket> \
  -f "where_host_ip_udids=<host_ip>,<udid>" \
  -f "where_status=active faulty maintenance" \
  -f status=disposed \
  -f "remark=<remark>"
```

5. On success, fetch the actual workflow run URL:

```bash
gh run list --repo LambdatestIncPrivate/migrations \
  --workflow realdevice-db-lmds-dispose-update.yml \
  --limit 1
```

Use the run ID from the output to construct the URL: `https://github.com/LambdatestIncPrivate/migrations/actions/runs/<run_id>`

Confirm and share this actual run URL (not the generic workflow URL).

6. **If the user provided a Slack thread URL**, post an approval request in that thread:
   - Parse `channel_id` and `thread_ts` from the URL (format: `/archives/<channel_id>/p<ts_no_dot>?thread_ts=<thread_ts>`)
   - Search for the approver's Slack user ID using `mcp__slack__users_search`
   - Post a reply in the thread using `mcp__slack__conversations_add_message` with:
     - List of disposed devices (IP + UDID)
     - Jira ticket link
     - GitHub Actions workflow URL
     - `@mention` of the approver requesting approval

   Example message:
   ```
   Raised disposal WF for the battery bloated devices:

   • `<host_ip>` — <udid>

   Jira: <jira_ticket>
   Workflow: https://github.com/LambdatestIncPrivate/migrations/actions/workflows/realdevice-db-lmds-dispose-update.yml

   @<approver> requesting your approval on this.
   ```

## Multiple Devices

If the user provides multiple devices, format `where_host_ip_udids` as space-separated pairs:

```
host_ip1,udid1 host_ip2,udid2
```
