variable "project_id" {
  description = "GCP project hosting the application."
  type        = string
}

variable "region" {
  description = "GCP region for regional resources."
  type        = string
  default     = "us-central1"
}

variable "environment" {
  description = "Deployment environment name."
  type        = string
  default     = "dev"
}

variable "project_name" {
  description = "Short, DNS-safe application name."
  type        = string
  default     = "pipeline"
}

variable "artifact_bucket_name" {
  description = "Globally unique Cloud Storage bucket for raw uploads and generated artifacts."
  type        = string
  default     = ""
}

variable "terraform_state_bucket_name" {
  description = "Globally unique Cloud Storage bucket used for this environment's Terraform state."
  type        = string
}

variable "serverless_vpc_subnet_cidr" {
  description = "Unused /26-or-larger subnet reserved for Cloud Run Direct VPC Egress."
  type        = string
}

variable "secret_ids" {
  description = "Secret Manager secret IDs keyed by their application purpose."
  type        = map(string)
  default = {
    "database-url"        = "database-url"
    "session-secret-key"  = "session-secret-key"
    "byok-encryption-key" = "byok-encryption-key"
    "clerk-secret-key"    = "clerk-secret-key"
  }
}

variable "container_image" {
  description = "Immutable container image URI deployed to Cloud Run."
  type        = string
}

variable "postgres_password" {
  description = "Password for the application Cloud SQL user. Supply outside source control."
  type        = string
  sensitive   = true
}

variable "session_secret_key" {
  description = "Starlette session signing secret. Supply outside source control."
  type        = string
  sensitive   = true
}

variable "byok_encryption_key" {
  description = "Fernet key used to encrypt organization BYOK credentials."
  type        = string
  sensitive   = true
}

variable "clerk_publishable_key" {
  description = "Clerk browser publishable key."
  type        = string
  default     = ""
}

variable "clerk_secret_key" {
  description = "Clerk backend secret key used to send account invitations."
  type        = string
  sensitive   = true
}

variable "clerk_issuer" {
  description = "Clerk JWT issuer URL."
  type        = string
  default     = ""
}

variable "clerk_audience" {
  description = "Clerk JWT audience configured on the application template."
  type        = string
  default     = ""
}

variable "clerk_jwks_url" {
  description = "Clerk JWKS endpoint used to verify session JWTs."
  type        = string
  default     = ""
}

variable "clerk_sign_in_url" {
  description = "Clerk hosted sign-in URL returning to the application callback."
  type        = string
  default     = ""
}

variable "clerk_jwt_template" {
  description = "Clerk JWT template name containing verified email claims."
  type        = string
  default     = "pipeline-beta"
}

variable "bootstrap_admin_emails" {
  description = "Verified Clerk emails allowed to bootstrap platform administration."
  type        = list(string)
  default     = []
}

variable "github_repository" {
  description = "GitHub repository allowed to deploy through workload identity, owner/name."
  type        = string
  default     = ""
}

variable "min_instances" {
  description = "Cloud Run minimum API instances."
  type        = number
  default     = 0
}

variable "max_instances" {
  description = "Cloud Run maximum API instances."
  type        = number
  default     = 10
}
