output "cloud_run_url" {
  description = "Public URL for the API service. Configure it as Clerk's callback host."
  value       = google_cloud_run_v2_service.app.urls[0]
}

output "artifact_registry_repository" {
  description = "Artifact Registry repository for application images."
  value       = google_artifact_registry_repository.app.name
}

output "artifact_bucket" {
  description = "Cloud Storage bucket for raw inputs and pipeline artifacts."
  value       = google_storage_bucket.artifacts.name
}

output "cloud_sql_connection_name" {
  description = "Cloud SQL connection name used by Cloud Run."
  value       = google_sql_database_instance.main.connection_name
}

output "github_workload_identity_provider" {
  description = "GitHub Actions workload identity provider resource name."
  value       = var.github_repository == "" ? null : google_iam_workload_identity_pool_provider.github[0].name
}

output "github_deployer_service_account" {
  description = "Service account impersonated by the GitHub deployment workflows."
  value       = var.github_repository == "" ? null : google_service_account.github_deployer[0].email
}
