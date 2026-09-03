# Azure Deployment & Setup Guide

This documents the self-hosted Azure deployment of Pipeline of Pipelines,
provisioned entirely with Terraform (`infra/`). It covers the layout, the
resources created, pre-requisites, per-environment configuration, Entra
ID/roles, and how to get from a fresh clone to a running environment.

> Hosting runs on **Azure App Service** (Linux, Standard S1) pulling a container
> from Azure Container Registry, with a **private** PostgreSQL Flexible Server.
> See [Architecture](architecture.md) and [Azure migration](azure_migration.md)
> for background and design rationale.

---

## 1. Repository layout

```
infra/
├── modules/
│   └── root/                 # Everything needed to deploy the app, as one module
│       ├── main.tf           #   resource group + shared locals/tags
│       ├── versions.tf       #   required providers + azurerm client config
│       ├── variables.tf      #   all inputs (parameterized)
│       ├── outputs.tf        #   useful values (app URL, KV id, etc.)
│       ├── networking.tf     #   VNet, App Service subnet, private PG subnet + DNS
│       ├── postgres.tf       #   PostgreSQL Flexible Server (private, pgvector)
│       ├── storage.tf        #   Storage account + artifact containers
│       ├── keyvault.tf       #   Key Vault + secrets (no creds in state/env)
│       ├── openai.tf         #   Azure OpenAI account + chat/embedding deployments
│       ├── acr.tf            #   Azure Container Registry
│       ├── entra.tf          #   Entra app registration, roles, group bindings
│       ├── app_service.tf    #   App Service Plan + Linux web app (fastapi container)
│       ├── identity.tf       #   system-assigned identity (ACR pull)
│       └── monitoring.tf     #   App Insights + Log Analytics + CPU alert
└── environments/
    └── dev/                  # One folder per environment (the "root" config)
        ├── main.tf           #   instantiates modules/root and forwards variables
        ├── backend.hcl       #   backend config (swap to Azurerm remote state here)
        ├── params.tfvars     #   non-secret environment parameters (committed)
        └── secrets.tfvars    #   secrets (gitignored, created from scratch)
```

**Key idea:** `modules/root` holds the full deployment; each `environments/<env>`
folder is a thin root config that supplies real values. Environments can be
separated later at subscription level; for now everything lives in one resource
group and one subscription.

---

## 2. Resources Terraform creates

| Resource | Notes |
| --- | --- |
| Resource group | `rg-<project>-<env>-<rand>` |
| App Service Plan | Linux, `S1` (default) |
| App Service (web app) | Linux container from ACR, system-assigned identity, `/health` probe |
| Azure Container Registry | Basic tier, admin disabled (managed-identity pull) |
| PostgreSQL Flexible Server | v16, `B_Standard_B1ms`, 32 GB, `pgvector`, **private** networking |
| Storage account | LRS, TLS 1.2, versioning, 4 artifact containers |
| Key Vault | Standard; holds DB URL, session secret, OpenAI key, storage conn str, Entra secret |
| Azure OpenAI | Cognitive account `S0`, `gpt-4o-mini` chat + `text-embedding-3-small` embedding deployments |
| Application Insights | wires into Log Analytics; Postgres CPU alert |
| VNet + subnets + private DNS | App Service regional VNet integration + private Postgres |
| Entra app registration + SP | app roles; **group** bindings for creator/reviewer/approver/admin |

**Secrets model:** no credential ever lands in Terraform state or inline env.
Secrets are written to Key Vault and exposed to the app via
`@Microsoft.KeyVault(SecretUri=...)` references, resolved by the App Service's
system-assigned identity.

---

## 3. Pre-requisites

Before you run anything you need:

1. **Terraform** (CI pins 1.16.1; >= 1.7 required):
   ```bash
   terraform version
   ```
2. **Azure CLI**, logged in with a subscription you can provision to:
   ```bash
   az login
   az account set --subscription "<your-subscription-id-or-name>"
   ```
3. **Your tenant / subscription IDs** and a **subscription where you can create**
   the resources (owner/contributor on the subscription or resource group).
4. **An Azure OpenAI resource** account with at least one key
   (`AZURE_OPENAI_API_KEY`) — you can either pre-provision one or supply the
   key after first apply. `params.tfvars` points the app at the Terraform-created
   OpenAI account.
5. **Push the container image** to ACR (or let CI build it):
   ```bash
   # after first apply, when the ACR login server is known:
   az acr login --name "<acr>"
   docker build -t <acr>.azurecr.io/pipeline-api:latest .
   docker push <acr>.azurecr.io/pipeline-api:latest
   ```
6. **An OpenAI-compatible base URL / key** for local dev (optional; local dev can
   use `OPENAI_API_KEY`).

---

## 4. Per-environment configuration

Each environment lives in `infra/environments/<env>/`. There are three files to
understand; only one is secret.

### 4.1 `params.tfvars` (committed, non-secret)

Fill in the values for your environment. Key entries:

```hcl
subscription_id = "0000...."
tenant_id       = "0000...."     # your Azure AD tenant id
entra_tenant_id = "0000...."     # same tenant, used for the OIDC authority

environment = "dev"
location    = "eastus"

project_name = "pop"
owner        = "you@example.com"

app_service_sku = "S1"        # Linux plan SKU
app_public_url  = ""          # set to the hostname POST-apply for prod redirect
container_image_tag = "latest"

database_username = "popadmin"
database_sku      = "B_Standard_B1ms"
database_version  = "16"
database_storage_mb = 32768

openai_chat_model      = "gpt-4o-mini"
openai_embedding_model = "text-embedding-3-small"
openai_api_version     = "2024-10-21"

# Entra groups -> app roles (see section 6)
role_group_object_ids = {
  admin    = ["<admin-group-guid>"]
  approver = ["<approver-group-guid>"]
  review   = ["<reviewer-group-guid>"]
  creator  = ["<creator-group-guid>"]
}
```

> `app_public_url` is left empty for dev (redirect URI = localhost). For a live
> env, set it to the App Service hostname after first apply so the Entra app
> registration gets the correct production redirect.

### 4.2 `secrets.tfvars` (gitignored, NOT committed)

Create this file from the example:
```bash
cp infra/environments/dev/secrets.tfvars.example infra/environments/dev/secrets.tfvars  # if you add an example
# or create it manually with:
vim infra/environments/dev/secrets.tfvars
```
```hcl
database_password = "your-strong-password"        # min 8 chars, upper+lower+number
azure_openai_api_key = "the-openai-key"
session_secret_key = "generate-me"                # see below
```
Generate a session secret:
```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

> The **Entra client secret is NOT in this file.** It is auto-generated by
> Terraform and stored in Key Vault. See section 5/6.

(.gitignore already excludes `infra/environments/*/secrets.tfvars`.)

### 4.3 `backend.hcl` (committed)

Holds the backend configuration for `terraform init`. Currently local:

```hcl
path = "terraform.tfstate"
```

Used with:
```bash
terraform init -backend-config=backend.hcl
```

---

## 5. Deploying an environment

From the environment directory:

```bash
cd infra/environments/dev

# 1. Configure the backend (local state for now)
terraform init -input=false -backend-config=backend.hcl

# 2. Preview
terraform plan -input=false \
  -var-file=params.tfvars \
  -var-file=secrets.tfvars

# 3. Apply
terraform apply \
  -var-file=params.tfvars \
  -var-file=secrets.tfvars
```

On success, useful values are printed by `terraform output`:

```bash
terraform output app_service_url        # https://<app>.azurewebsites.net
terraform output acr_login_server       # acr to push the container to
terraform output postgres_fqdn
terraform output entra_application_id
terraform output key_vault_id
```

**First-run order of operations (if the app must come up end-to-end):**

1. `terraform apply` (creates everything except a working container).
2. Push your built image: `az acr login` + `docker build/push` to the ACR
   login server from `terraform output acr_login_server`.
3. If you didn't set `app_public_url` before apply, set it now to the App
   Service hostname and re-apply so the Entra redirect URI is correct.
4. Build/verify the container — the App Service health check hits `/health`
   (`backend/src/routers/api.py`).

> **Note on the App Service ↔ ACR pull:** this setup relies on the App Service's
> system-assigned identity + the `AcrPull` role + `linux_fx_version =
> DOCKER|...`. This is the documented azurerm 4.x pattern. If the container
> fails to start on first apply, see section 9 (troubleshooting).

---

## 6. Entra ID: login, roles, and groups

### 6.1 What Entra does here

Entra ID (Azure AD) is the **authentication + authorization** provider. It
replaces WorkOS. After the OAuth authorization-code + PKCE flow, the app reads
identity claims (`email`, `name`, `oid`) and the `roles` claim
(`backend/src/auth_service.py`).

### 6.2 What Terraform provisions

- `azuread_application.app` — app registration (client id, redirect URIs, 4 app
  roles: `creator`, `reviewer`, `approver`, `admin`).
- `azuread_application_password.app` — an auto-generated client secret. Written
  straight to Key Vault (`entra-client-secret`) and surfaced to the app as
  `ENTRA_CLIENT_SECRET` via a Key Vault reference. **You never supply or see
  this secret**; it's managed entirely by Terraform + Key Vault.
- `azuread_service_principal.app` — the service principal.
- `azuread_app_role_assignment.role[*]` — binds **Entra groups** to app roles,
  driven by the `role_group_object_ids` map in `params.tfvars`.

### 6.3 How role assignment works (groups)

Terraform encodes **role → group** in `params.tfvars`:

```hcl
role_group_object_ids = {
  admin    = ["<admin-group-guid>"]
  approver = ["<approver-group-guid>"]
  creator  = ["<creator-group-guid>"]
}
```

- Terraform creates one `azuread_app_role_assignment` per (role, group) pair.
- **Which users are in each group is managed in the Entra portal** — how you
  grant a person the admin/creator/etc. role is entirely up to you in the portal.
  Credentials never touch Terraform.
- Empty map (`{}`) → no assignments, and a bare clone still `validate`s clean.

### 6.4 Getting group object IDs

```bash
# The app role is assigned to a GROUP, so find each group's object ID:
az ad group show --group "<group-name>" --query id -o tsv
```

Put those GUIDs into `params.tfvars` under the appropriate role key, then
re-apply Terraform.

### 6.5 Assigning users to groups (in the portal)

1. Portal → Microsoft Entra ID → **Groups** → open the group (e.g. `pop-admins`).
2. **Members** → Add → search/select users.
3. Those users now carry the app's `admin` role on the `roles` claim after
   sign-in.

### 6.6 The three Entra env vars the app needs

All are wired automatically by Terraform into the App Service `app_settings`:

| Env var | Source |
| --- | --- |
| `ENTRA_CLIENT_ID` | `azuread_application.app.client_id` (auto) |
| `ENTRA_TENANT_ID` | `entra_tenant_id` in `params.tfvars` |
| `ENTRA_CLIENT_SECRET` | auto-generated; Key Vault reference |

---

## 7. Accessing secrets (Key Vault)

All runtime secrets live in **Azure Key Vault** (created by Terraform). Retrieve
them via CLI or portal:

```bash
# list the vault (e.g. from the output, or find it):
az keyvault list -o table

# read a secret's value:
az keyvault secret show --vault-name "<kv-name>" --name "<secret-name>" \
  --query value -o tsv
```

Common secret names:

| Secret name | Contains |
| --- | --- |
| `database-url` | Postgres connection string (with password) |
| `entra-client-id` | Entra app client id |
| `entra-client-secret` | Auto-generated client secret |
| `session-secret` | Session signing key |
| `azure-openai-api-key` | Azure OpenAI key |
| `azure-storage-connection-string` | Storage account connection string |

> These are **never** in Terraform state. The app reads them at runtime via Key
> Vault references using the App Service's system-assigned identity.

---

## 8. Local development (no Azure)

For local dev you don't need any of the Azure infrastructure — auth is bypassed:

```bash
export OPENAI_API_KEY="sk-..."
export OPENAI_BASE_URL="https://api.openai.com/v1"
export AUTH_BYPASS_LOCAL="true"
uv run uvicorn app:app --app-dir backend/src --reload
```

Lineage, object-store, and artifact-store run on the local filesystem
(`data/`) for dev. See the root [README](../README.md) for the full local setup.

---

## 9. Troubleshooting

**App Service container won't start / 500 on `/health`**
- Confirm an image is pushed: `terraform output acr_login_server`, then
  `az acr repository list -n "<acr>"`.
- Confirm the container pulls via the managed identity: the App Service's
  system-assigned identity must hold `AcrPull` on the registry (provisioned by
  `identity.tf`).
- Check the App Service logs in the portal (App Service → Log stream) or App
  Insights. If the pull/auth fails, add `DOCKER_REGISTRY_SERVER_URL` /
  `DOCKER_REGISTRY_SERVER_USERNAME`/`DOCKER_REGISTRY_SERVER_PASSWORD` app
  settings as a fallback (not normally needed).

**App signs out / can't reach Entra**
- Ensure `ENTRA_TENANT_ID` and the redirect URI match the app registration.
  `ENTRA_REDIRECT_URI = https://<hostname>/auth/callback` is generated from the
  App Service hostname automatically.
- To use a custom domain, set `app_public_url` to the FQDN and re-apply.

**Postgres unreachable from the app**
- Relies on the App Service regional VNet integration (`azurerm_app_service_
  virtual_network_swift_connection`) plus the private DNS zone link. Confirm both
  exist in the portal (App Service → Networking, and the private DNS zone).

**Rotating a secret**
- Session secret, OpenAI key, storage connection string: update the value in the
  **Key Vault secret** (portal or CLI). The app picks it up without a redeploy.
- DB password / connection string: rotate in Postgres and/or re-apply Terraform
  with the new `database_password`, which rewrites the `database-url` KV secret.

**State / backend**
- To move from local state to **Azure remote state**, replace the contents of
  `backend.hcl` with the `azurerm` backend arguments (e.g. `resource_group_name`,
  `storage_account_name`, `container_name`, `key`) and run:
  ```bash
  terraform init -backend-config=backend.hcl -reconfigure
  ```
  Terraform moves the state for you.

---

## 10. CI / pre-commit

- `.github/workflows/ci.yml` runs a `terraform` job that does
  `fmt -check -recursive` on `infra/`, then `init -backend=false` and `validate`
  inside `infra/environments/dev`.
- `.pre-commit-config.yaml` and the repo `./pre-commit` hook run
  `terraform fmt -check -recursive infra/` alongside the Python linters.
- Commands run against the **dev environment** root; add per-env validate steps
  if you add `staging`/`prod`.

---

## 11. Adding an environment (e.g. staging/prod)

1. Copy the dev env folder:
   ```bash
   cp -r infra/environments/dev infra/environments/staging
   ```
2. Edit `infra/environments/staging/params.tfvars` for the new values
   (`environment = "staging"`, `app_public_url`, SKUs, group GUIDs).
3. Recreate `secrets.tfvars` with staging secrets (do not reuse dev secrets).
4. Run `terraform init -backend-config=backend.hcl` and apply from that folder.
   Because state is per-environment (each folder has its own `terraform.tfstate`),
   the environments are independent.
5. Optionally point the env at a different subscription (change
   `subscription_id`); all resources are already scoped to the env's resource
   group, so subscription separation later is a config change.