# RULE: Credential Handling

## Never Ask User for Credentials

Do **NOT** ask users to paste API keys, OAuth client IDs, client secrets, passwords, or any authentication credentials in chat.

## Never Read Credentials

Do **NOT** read credential values from `.env` files, environment variables, or any configuration files. Do not cat, grep, or otherwise display credential values.

- Do **NOT** cat `.env` to check credential values
- Do **NOT** grep `.env` for credential values
- Do **NOT** `docker compose exec` printenv to view credential values
- Do **NOT** log or print credential values in any form
- Credentials may be **present** or **absent** (empty string), but never **read** their values

### How to Handle Credentials Without Reading Them

- To **add** credentials: User edits `.env` directly
- To **clear** credentials: Set value to empty string in `.env`, restart service
- To **verify presence**: Check that the line exists, not that it has a value
- To **restart**: `docker compose up -d <service>`

### When Credentials Fail or Are Missing

1. **Clear the credentials** from all config files (`.env`, task definitions, etc.)
2. **Restart** the affected service
3. **Notify** the user: "Credentials cleared. Please add your [X] credentials to .env and restart."
4. **Do NOT retry** the same credentials - they are invalid/revoked

### Never Store Credentials in Chat

- Do not paste client IDs, secrets, passwords, or API keys in chat messages
- Do not ask users to paste them
- If credentials are needed, have the user edit their local `.env` file directly
