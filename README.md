# soapalin

CLI tool for duplicating, provisioning, and migrating Zimbra servers.

The source server is always read-only. The destination is additive only: data is
created or updated, never deleted automatically. The tool supports account,
calendar-resource, and distribution-list migrations.

## Features

- Multi-server configuration with `config/servers/<label>.json`.
- Read-only source and destination inventories.
- Provisioning for domains, COS, distribution lists, resources, and accounts.
- Mailbox, calendar, contacts, tasks, briefcase, tags, and settings migration.
- Outgoing shares and accepted incoming mountpoints.
- Empty-folder creation, folder colors, and custom tag colors.
- Background jobs with logs, status, and resume support.
- Cleanup for temporary data and destination share mountpoints.

## Installation

Requirement: Python 3.8 or newer.

### Linux / Zimbra

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .

# Or install on a Zimbra server
sudo ./install.sh
```

### Windows

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .

# Or install locally
.\install.ps1
```

### Development

```bash
python -m pip install -e ".[dev]"
pytest
ruff check soaplun tests
mypy soaplun
```

The `install.sh`, `install.ps1`, `uninstall.sh`, and `uninstall.ps1` scripts
provide standalone installation on a Zimbra server or Windows.

## Quick start

```bash
soapalin server add src1 --role source
soapalin server add dst1 --role destination

soapalin server inventory src1
soapalin server inventory dst1

soapalin provision users.csv \
  --type account \
  --from src1 \
  --to dst1 \
  --password "<PASSWORD>"

soapalin migrate users.csv \
  --type account \
  --item mail,calendar,settings \
  --from src1 \
  --to dst1
```

## Servers

```bash
soapalin server add src1 --role source
soapalin server add src1 --role source \
  --host zimbra-src.local --port 7071 \
  --admin admin --password "<PASSWORD>" --no-test
soapalin server list
soapalin server show src1
soapalin server test src1
soapalin server inventory src1
soapalin server inventory src1 --stats
soapalin server inventory src1 --stats --parallelism 4
soapalin server inventory src1 --follow
soapalin server rename src1 zimbra-prod
soapalin server remove src1
```

Inventories skip mailbox statistics by default. `--stats` enables `GetMailbox`
for every account to collect mailbox sizes and message counts. `--no-stats`
remains accepted for compatibility.

## Provisioning

```bash
soapalin provision domains.csv --type domain --from src1 --to dst1
soapalin provision all --type cos --from src1 --to dst1
soapalin provision lists.csv --type dl --from src1 --to dst1
soapalin provision resources.csv --type resource --from src1 --to dst1

soapalin provision users.csv --type account --from src1 --to dst1 --password "<PASSWORD>"
soapalin provision users.csv --type account --from src1 --to dst1 --password-file pwd.txt
soapalin provision users.csv --type account --from src1 --to dst1 --random-password --keep-admin
```

Use `all` instead of a CSV to select every source object. Available mappings are
`--mapping-domain` and `--mapping-host`.

## Migration

```bash
soapalin migrate users.csv --type account \
  --item mail,calendar,settings \
  --from src1 --to dst1

soapalin migrate all --type account --item all --from src1 --to dst1
soapalin migrate resources.csv --type resource --item mail,calendar --from src1 --to dst1
soapalin migrate lists.csv --type dl --item members --from src1 --to dst1
soapalin migrate all --type all --item all --from src1 --to dst1
```

`--item` is required. Values are comma-separated; spaces around commas are
accepted.

Valid elements:

```text
account  : mail calendar contacts tasks briefcase cos folder color settings
           tags filter banlist whitelist share sendas sendonbehalfof
resource : mail calendar folder share
dl       : members
```

With `--type all`, a partial account CSV is allowed. Accounts are filtered by
the CSV; resources and distribution lists are selected with `all`.

### COS

The `cos` element applies the source COS to existing accounts. Create the COS
on the destination first, then refresh the destination inventory:

```bash
soapalin --foreground provision all --type cos --from src1 --to dst1
soapalin --foreground server inventory dst1
soapalin --foreground migrate users.csv --type account --item cos --from src1 --to dst1
```

### Shares

`share` copies outgoing shares and recreates incoming shares already accepted
on the source. Existing destination mountpoints are kept and their colors are
synchronized.

### Folders and colors

- `folder` creates only the missing empty folder hierarchy.
- `color` applies colors to existing folders only.
- When both are selected, `folder` runs before `color`.

```bash
soapalin migrate users.csv --type account --item folder --from src1 --to dst1
soapalin migrate users.csv --type account --item color --from src1 --to dst1
soapalin migrate users.csv --type account --item folder,color --from src1 --to dst1
```

### Differential and retry

```bash
soapalin migrate users.csv --type account \
  --item mail,calendar,settings \
  --from src1 --to dst1 --differential

soapalin migrate users.csv --type account \
  --item mail \
  --from src1 --to dst1 --retry-failed
```

## Simulation and options

Global options go before the subcommand:

```bash
soapalin --dry-run migrate users.csv --type account --item all --from src1 --to dst1
soapalin --foreground migrate users.csv --type account --item mail --from src1 --to dst1
```

Migration options:

```text
--differential [YYYY-MM-DD]
--retry-failed [REPORT]
--parallelism N
--keep-tgz [true|false]
--keep-tgz-days N
--mapping-domain CSV
--mapping-host CSV
--resume
--json
```

`--json` is allowed only with `--dry-run`.

## Jobs

Provisioning, migrations, and inventories run as background jobs by default.

```bash
soapalin job list
soapalin job list --running
soapalin job list --failed
soapalin job status JOB_ID --tail 30
soapalin job log JOB_ID
soapalin job live
soapalin job stop all
```

Job state and logs are stored in `data/jobs/<id>/`.

## Cleanup

Temporary data:

```bash
soapalin clean --dry-run
soapalin clean mailboxes --older-than 7
soapalin clean all --older-than 30 --keep-last 5
soapalin clean snapshots --keep-last 10
```

Destination shares:

```bash
soapalin clean --share users.csv --type account --to dst1 --dry-run
soapalin clean --share --type account --to dst1 --parallelism 10
soapalin clean --share users.csv --type account --from src1 --to dst1
```

Without `--from`, all incoming mountpoints for the selected accounts are
removed. With `--from`, only mountpoints absent from the source are removed.

## Local data

```text
config/config.json       global configuration
config/servers/          source/destination servers
data/snapshots/          inventories
data/mailboxes/          temporary TGZ archives
data/reports/            JSON/CSV reports and backups
data/jobs/               job state and logs
data/trash/              archives moved by --directly-clean
```

These directories are ignored by Git.

## Tests and quality

```bash
pytest
pytest --cov=soaplun --cov-report=term-missing
ruff check soaplun tests
mypy soaplun
python -m unittest discover -s tests
```

## GitHub integration

The repository is ready for GitHub Actions:

```text
.github/workflows/ci.yml       tests, lint, and build
.github/ISSUE_TEMPLATE/        bug and feature templates
.github/pull_request_template.md
CONTRIBUTING.md
.gitattributes
.editorconfig
```

## Contributing

See `CONTRIBUTING.md`.
