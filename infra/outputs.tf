output "cloud_run_url" {
  description = "Public URL for the API service. Configure it as Clerk's callback host."
  value       = google_cloud_run_v2_service.app.uri
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
