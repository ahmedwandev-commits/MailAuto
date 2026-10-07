# SA User Clone — Oracle User Provisioning SQL Generator

`provision_users.py` reads a list of user IDs (Excel / `--users` / `--users-file`),
pulls each user's full record from the **SOURCE** Oracle DB (UAT by default) and
writes ready-to-run SQL\*Plus scripts that re-provision those users on the
**TARGET** Oracle DB (BS / `fcshr` by default).

For every user the generated SQL does **delete-then-insert inside one PL/SQL
transaction**: the user is deleted from all configured tables (children first,
`SSZB_USER` last) and then re-inserted fresh from SOURCE. Any error rolls that
user back and the run continues. The script itself only ever runs `SELECT`
against SOURCE — it never changes Oracle data; you run the generated `.sql`
files on TARGET.

## Tables cloned

Explicitly configured (`SINGLE_ROW_TABLES` / `MULTI_ROW_TABLES`):

```
SSZB_USER  SMZB_USER  SMZB_USERLOG_DETAILS  SMZB_USER_ALLOW_UDE_CUSTOM
SMZB_DASHBOARD_MASTER  COZM_USER_DETAILS  SMTB_USER_CUSTOM  SMZB_USER_ENTITY
SMZB_MSGS_RIGHTS  SMZB_USER_ROLE  SMZB_USER_CENTRAL_ROLE  SMZB_USER_CENTRAL_ROLES
SMZB_USER_ACCCLASS  SMZB_USER_BRANCHES
```

Additional PDATA tables (`ADDITIONAL_TABLES`) — their user-id column and
primary key are **auto-discovered** from the Oracle data dictionary at run
time, so no column names are hard-coded and a table that is missing in the
source schema is simply skipped:

```
SMZB_DASHBOARD_DETAILS  SMZB_QUEUE_RIGHTS  SMZB_USER_ACCESS_PRODUCTS
SMZB_USER_FUNC_DISALLOW  SMZB_USER_GLEXCEPT  SMZB_USER_GLREST  SMZB_USER_GROUP
SMZB_USER_HOTKEY  SMZB_USER_LIMITS_ROLE  SMZB_USER_PRODUCTS  SMZB_USERS_FUNCTIONS
SMZB_USER_DETAIL  SMZM_USER_NETWORK_DET_CUSTOM  SMZM_USER_SOURCE_DET_CUSTOM
```

Rights tables keyed on `USER_ROLE_ID` that also have a `USER_ROLE_FLAG` column
(e.g. `SMZB_MSGS_RIGHTS`, `SMZB_QUEUE_RIGHTS`) are copied/deleted with the
`USER_ROLE_FLAG = 'U'` filter so only the user's own row is touched, never a
role's row.

To add or remove a table later, edit the `ADDITIONAL_TABLES` list only.

## Requirements

```
pip install oracledb openpyxl
```

## Credentials (no password is stored in the script)

Source DB credentials are read from environment variables:

| Variable     | Meaning                     | Default                      |
|--------------|-----------------------------|------------------------------|
| `SRC_USER`   | source DB user              | *(required)*                 |
| `SRC_PWD`    | source DB password          | *(required)*                 |
| `SRC_DSN`    | source DB DSN               | `fcuatdb-scan:1521/PRODPDB`  |
| `SRC_SCHEMA` | schema owning the tables    | `FCPROD`                     |
| `TGT_DSN`    | target DSN (header only)    | `fcshr-scan:1521/PRODPDB`    |
| `TGT_SCHEMA` | schema written into the SQL | `FCPROD`                     |

```bat
rem Windows cmd
set SRC_USER=your_user
set SRC_PWD=your_password
set SRC_DSN=fcuatdb-scan:1521/PRODPDB
```

```bash
# Linux / macOS
export SRC_USER=your_user SRC_PWD=your_password SRC_DSN=fcuatdb-scan:1521/PRODPDB
```

## Run

```bash
python provision_users.py --date 2026-06-01
python provision_users.py --users SAUSER1,SAUSER2 --date 2026-06-01
python provision_users.py --excel "C:\path\Users.xlsx" --date 2026-06-01
```

Output (next to the script):

- `generated_sql/generated_users_part001.sql`, `part002.sql`, … — run on TARGET in order
- `generated_users_report.csv` — summary of what was found

After running on TARGET, search the spool for `FAILED` to find any user whose
transaction rolled back.
