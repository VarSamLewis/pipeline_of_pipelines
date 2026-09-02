# Local backend for the dev environment.
#
# Used by: terraform init -backend-config=backend.hcl
# (key/value partial config; the local backend takes a single "path" argument)
#
# To migrate to Azure remote state later, replace this file's contents with
# the Azurerm backend arguments, e.g.:
#
#   resource_group_name  = "rg-tfstate"
#   storage_account_name = "tfstatepopdev"
#   container_name       = "tfstate"
#   key                  = "dev/pop.tfstate"
#
# then re-run: terraform init -backend-config=backend.hcl -reconfigure

path = "terraform.tfstate"
