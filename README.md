# CLI for YNAB

A JSON CLI for YNAB budgets. Every operation in YNAB's
official Python SDK is available, alongside convenience commands for categorizing,
allocating money, setting targets, and reporting. This project is independent of YNAB.

## Install

```sh
brew install dtour/tools/cli-for-ynab
ynab --help
ynab guide
```

The Homebrew package supports macOS 14 or later on Apple Silicon and Intel.
The package is named `cli-for-ynab`; its command is `ynab`.
Upgrade with `brew update` followed by `brew upgrade dtour/tools/cli-for-ynab`.

## Development

```sh
uv sync --locked
uv run ynab --help
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Python 3.12 or later. Runtime dependencies are locked in `uv.lock`. The Python
module is `ynab_cli`; the executable is `ynab`.

## Authentication and budgets

Create a personal access token in [YNAB Developer Settings](https://app.ynab.com/settings/developer).
Enter it directly into the hidden terminal prompt:

```sh
ynab auth login
ynab auth status
ynab budgets list
ynab budgets set-default BUDGET_ID
ynab budgets alias NAME BUDGET_ID  # optional
```

Credentials are stored in macOS Keychain through `keyring`. For unattended
environments, `YNAB_API_TOKEN` takes precedence over Keychain. `--token-stdin`
accepts a token through stdin; there is no token argument to expose in shell
history. `auth logout` removes the stored credential; it cannot unset an
environment variable in the parent shell or revoke a token at YNAB.

Configuration stores aliases and the default budget, never the token. Its
directory follows `platformdirs`, or `YNAB_CONFIG_DIR` when supplied.
On macOS it is `~/Library/Application Support/cli-for-ynab/`.
Tokens saved by the earlier local `ynab-cli-python` build remain readable.
Budget selection precedence is `--budget`, `YNAB_BUDGET_ID`, then the saved
default. Explicit selection is recommended for agents working across budgets.
Put global options **before** the command: `ynab --budget BUDGET_ID ...`.

## Inspect and categorize

```sh
ynab --budget BUDGET_ID accounts list
ynab --budget BUDGET_ID categories list
ynab --budget BUDGET_ID transactions list --type uncategorized --limit 0
ynab --budget BUDGET_ID transactions list --since 2026-09-01 --until 2026-09-30 \
  --fields id,date,payee_name,amount,category_id,memo
ynab --budget BUDGET_ID transactions categorize TRANSACTION_ID --category CATEGORY_UUID --dry-run
ynab --budget BUDGET_ID transactions categorize TRANSACTION_ID --category CATEGORY_UUID
ynab --budget BUDGET_ID transactions categorize-batch --file categorizations.json
```

Batch input is an array of up to 100 entries containing only `id` and
`category_id`. Use `--file -` to read stdin:

```json
[{"id":"transaction-id","category_id":"33333333-3333-4333-8333-333333333333"}]
```

Routine categorization changes only the category. It validates the entire batch
before writing, preserves other fields, skips records already categorized as
requested, and refuses to replace split or transfer structures. It never deletes
and recreates a transaction. It does not make categorization decisions itself;
the agent supplies the mapping.

Transaction lists default to the past 365 days and 100 rows. `--limit 0` returns
all matching rows. Metadata includes the date range, row counts, and truncation.
An explicit `--since` is needed for older transactions. Server delta cursors are
returned only for complete, unfiltered results. To continue a delta query, use
the same date range and API type filter with `--knowledge N --limit 0`, without
local filters or field selection. Deleted records are retained as tombstones.

## Review allocations and targets

```sh
ynab --budget BUDGET_ID categories view CATEGORY_UUID --month 2026-10
ynab --budget BUDGET_ID categories allocate CATEGORY_UUID --month 2026-10 \
  --amount 500.00 --save allocation.json
ynab --budget BUDGET_ID categories move --from SOURCE_UUID --to DESTINATION_UUID \
  --month 2026-10 --amount 50.00 --save move.json
ynab --budget BUDGET_ID categories target CATEGORY_UUID --amount 300.00 \
  --frequency monthly --rollover refill --save target.json
ynab --budget BUDGET_ID categories target CATEGORY_UUID --amount 1200.00 \
  --due 2027-06-01 --save dated-target.json
```

These commands produce proposals and do not change YNAB. `allocate --amount`
sets the **total assigned amount** for that category and month. `move` subtracts
from one category and adds to another within the same budget and month.
`--frequency` supports monthly, weekly, and yearly targets; `--due` sets a target
date. `--rollover refill` or `add` controls the API's refill/set-aside behavior.
Loan payment targets accept an amount only. Other target combinations remain
subject to YNAB's API validation. The API does not expose every target option
available in YNAB's UI.

After the user reviews the proposal's budget, categories, values, and effects:

```sh
ynab changes apply move.json --approve FULL_PROPOSAL_ID
```

Copy the ID from the proposal. The digest identifies the exact reviewed content;
it is not proof of human approval. Agents must obtain the user's approval before
calling `apply`. The saved canonical budget ID is used even if the default later
changes. Supplying a conflicting `--budget` is an error. Edited or stale proposals
are rejected. Files created by `--save` have owner-only permissions and existing
files are not overwritten.

YNAB has no atomic multi-category update or compare-and-swap operation. The CLI
checks all preconditions before applying. Concurrent changes after that check
remain possible. Category moves use two writes; on failure, the error includes
completed operations and whether the failing write has an uncertain outcome.
It does not automatically retry or roll back. Inspect YNAB before continuing.
Reapplying an allocation proposal skips categories already at the requested
assigned amount; the remaining preconditions must still match.

## Spending reports

```sh
ynab --budget BUDGET_ID reports spending --month 2026-09
ynab --budget BUDGET_ID reports spending --since 2026-01-01 --until 2026-09-30 --group-by month
ynab --budget BUDGET_ID reports spending --month 2026-09 --group-by payee
```

Reports count split children once, subtract categorized refunds, exclude deleted
transactions, tracking-account activity, internal inflows and uncategorized
positive inflows. Transfers between budget accounts are excluded. Categorized
outflows to tracking accounts count as spending. Results disclose exclusion counts.
Reports are calculated from transactions; they are not statements of available
cash, allocations, or targets. Each report belongs to one budget and identifies
its currency. Amounts from different budgets/currencies are never combined.

## Complete API access

Version 0.2.1 covers all **44 operations** in the pinned `ynab==4.4.0` SDK:
28 reads and 16 writes across 10 API classes. A coverage test compares the
registered operations with the installed SDK, so upgrades expose any gaps.

| Resource | Operations | Includes |
| --- | ---: | --- |
| User | 1 | Authenticated user |
| Plans | 3 | Budget summaries, full export, settings |
| Accounts | 3 | List, retrieve, create |
| Categories | 8 | Categories, groups, targets, monthly allocations |
| Months | 2 | Budget-month summaries and details |
| Payees | 4 | List, retrieve, create, update |
| PayeeLocations | 3 | Locations by budget, payee, or ID |
| MoneyMovements | 4 | Movement history and groups, including month filters |
| Transactions | 11 | Queries, creation, updates, deletion, linked-account import |
| ScheduledTransactions | 5 | List, retrieve, create, update, delete |

Discover operations and their exact JSON input schemas without credentials:

```sh
ynab api list
ynab api list --resource scheduled
ynab --pretty api schema create_scheduled_transaction
ynab --pretty api schema update_transaction
```

Operation names match the SDK. Hyphenated spellings also work. `--params` supplies
path/query parameters, and `--data` supplies the complete SDK request wrapper.
Both accept a JSON literal, `@file.json`, or `-` for stdin; only one may use stdin.
Budget selection always comes from the root `--budget` option or saved preference;
`plan_id` is not accepted inside `--params`. The API calls budgets "plans."

```sh
ynab api call get_plans --params '{"include_accounts":true}'
ynab --budget BUDGET_ID api call get_scheduled_transactions
ynab --budget BUDGET_ID api call get_plan_month --params '{"month":"2026-10"}'
ynab --budget BUDGET_ID api call get_transactions_by_account --params @query.json
```

For `query.json`, use the schema's names:

```json
{"account_id":"55555555-5555-4555-8555-555555555555","since_date":"2026-01-01","until_date":"2026-09-30"}
```

API reads return all rows supplied by the endpoint without the convenience
command's 100-row limit or local filters. Server defaults still apply, including
the one-year default for transaction queries. Supply explicit date ranges when
needed. Delta cursors are returned unchanged; keep the original query parameters
when continuing. `--dry-run` validates a read request without contacting YNAB.

All JSON monetary inputs use **integer milliunits**, including `amount`,
`balance`, `budgeted`, and `goal_target`. Use public JSON field names such as
`date`, not SDK Python aliases such as `var_date`. Unknown fields, including nested
fields, are rejected. Omitted fields stay omitted, and explicit `null` and empty
arrays are preserved. Month parameters accept `YYYY-MM`, `YYYY-MM-01`, or `current`;
`current` is resolved using UTC before a write proposal is saved.

### API writes and approval

```sh
ynab --budget BUDGET_ID api call create_payee \
  --data '{"payee":{"name":"New cafe"}}' --save new-payee.json
ynab --budget BUDGET_ID api call create_scheduled_transaction \
  --data @scheduled.json --save scheduled-proposal.json
ynab --budget BUDGET_ID api call delete_transaction \
  --params '{"transaction_id":"TRANSACTION_ID"}' --save deletion.json
ynab --budget BUDGET_ID api call import_transactions --save import.json
```

These return proposals. Review the budget, operation, parameters, request body,
and existing record (`before`), then use the same apply command as convenience
proposals:

```sh
ynab changes apply new-payee.json --approve FULL_PROPOSAL_ID
```

YNAB's built-in payees ("Starting Balance", "Manual Balance Adjustment" and
"Reconciliation Balance Adjustment") are rejected by YNAB as `payee_name`, so the CLI
refuses them while building the proposal (`reserved_payee`); use an ordinary payee name.

Updates and deletions snapshot their existing records and reject stale proposals
before writing. Creations have no existing record to snapshot. An import proposal
authorizes requesting the pending imports from linked accounts; the API cannot
preview the individual transactions it will import. YNAB remains responsible for
API validation and unsupported behaviors, including edits to existing split
subtransactions. The CLI never deletes and recreates a split to work around this.

**Automatic exception:** `update_transaction` with only `category_id`, or
`update_transactions` with only `id` and `category_id` per entry (up to 100), uses
the routine categorization policy. It validates categories and refuses splits or
transfers, just like the convenience commands. Add `--dry-run` to preview it;
`--save` forces the approval-proposal workflow. Clearing a category with `null`,
looking up by `import_id`, and changing any additional field require approval.

API proposals use schema version 2; existing allocation/target proposals remain
version 1. API proposals are attempted at most once per local configuration
directory. An owner-only receipt is written before sending; duplicate attempts,
including concurrent attempts, return `already_attempted`. After a failed or
uncertain attempt, inspect YNAB and obtain approval for a fresh proposal. A rejected
write reports YNAB's own explanation in `error.details.reason` (tokens redacted).
Receipts live in the configuration directory's `changes/` subfolder and contain
status metadata, not request bodies or tokens. This is a local replay guard;
another computer or configuration directory has separate receipts. The digest
identifies reviewed content and does not prove that a human approved it.

## JSON and amounts

Success is `{"data": ..., "meta": ...}` on stdout. Add global `--pretty` for
indentation. Errors are `{"error":{"code":...,"message":...}}` on stderr with
a nonzero exit code. Help, version and the agent guide are available without
credentials. Exit codes: 1 general/API error, 2 input/configuration error,
3 authentication/authorization, 4 missing/conflicting state, 5 network/rate limit.

Convenience `--amount` flags use the budget's currency, e.g. `--amount 12.50`. JSON monetary
values are integer **milliunits**: `12500` means 12.50. No floating-point conversion
is used. API display duplicates ending `_currency` or `_formatted` are removed.
Report fields ending `_decimal` are exact decimal strings for display. Values with
more than three decimal places are rejected rather than rounded.

Transport timeouts are 10 seconds to connect and 30 seconds to read; automatic
retries are disabled. Authentication errors and API messages are sanitized. The
CLI does not log tokens, request headers, or tracebacks.

## Agents

Run `ynab guide` or read [the agent guide](docs/agents.md). The same CLI works with
any agent that has shell access to the installed executable. Routine
categorization may be automatic; allocations and target changes require review.

## Maintenance and releases

Source, issues, and releases live in [dtour/cli-for-ynab](https://github.com/dtour/cli-for-ynab).
The Homebrew formula lives in [dtour/homebrew-tools](https://github.com/dtour/homebrew-tools).
See [the maintenance guide](docs/maintenance.md) for dependency updates, release
commands, and the layout of the project.

CI runs lint, tests, and builds on Linux and macOS with Python 3.12 and 3.14.
Version tags trigger a release workflow that publishes the wheel, source archive,
checksums, and a generated Homebrew formula. The formula pins dependency wheels
from `uv.lock` inside a private Homebrew environment using Python 3.14.

The test suite uses synthetic data and a mocked transport, blocks network access,
and isolates configuration and credential storage. Live validation requires a
locally supplied token and a disposable test budget.

## Attribution

CLI for YNAB is an independent project, without affiliation, endorsement, or
support from YNAB. YNAB and You Need A Budget are trademarks of YNAB.
Visit [YNAB](https://www.ynab.com/) for the official product.
