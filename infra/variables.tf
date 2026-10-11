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

variable "openai_api_key" {
  description = "Optional platform OpenAI API key. Supply outside source control."
  type        = string
  sensitive   = true
  default     = ""
}

variable "clerk_publishable_key" {
  description = "Clerk browser publishable key."
  type        = string
  default     = ""
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
