locals {
  name = "${var.project_name}-${var.environment}"
  labels = {
    application = var.project_name
    environment = var.environment
    managed-by  = "terraform"
  }
  required_apis = toset([
    "artifactregistry.googleapis.com",
    "compute.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "servicenetworking.googleapis.com",
    "sqladmin.googleapis.com",
    "storage.googleapis.com",
    "aiplatform.googleapis.com",
  ])
}

resource "google_project_service" "required" {
  for_each           = local.required_apis
  service            = each.value
  disable_on_destroy = false
}

resource "google_service_account" "runtime" {
  account_id   = "${local.name}-runtime"
  display_name = "${local.name} Cloud Run runtime"
}

resource "google_artifact_registry_repository" "app" {
  location      = var.region
  repository_id = local.name
  description   = "Container images for ${local.name}"
  format        = "DOCKER"
  labels        = local.labels

  depends_on = [google_project_service.required]
}

resource "google_storage_bucket" "artifacts" {
  name                        = "${var.project_id}-${local.name}-artifacts"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false
  labels                      = local.labels

  versioning {
    enabled = true
  }

  lifecycle_rule {
    action { type = "Delete" }
    condition {
      age            = 90
      with_state     = "ARCHIVED"
      matches_prefix = ["raw-files/", "output-folders/", "target-schemas/"]
    }
  }
}

resource "google_compute_network" "main" {
  name                    = "${local.name}-vpc"
  auto_create_subnetworks = false
}

resource "google_compute_global_address" "private_services" {
  name          = "${local.name}-private-services"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 16
  network       = google_compute_network.main.id
}

resource "google_service_networking_connection" "private_services" {
  network                 = google_compute_network.main.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_services.name]
}

resource "google_sql_database_instance" "main" {
  name                = "${local.name}-postgres"
  database_version    = "POSTGRES_16"
  region              = var.region
  deletion_protection = true

  settings {
    tier              = "db-custom-1-3840"
    availability_type = var.environment == "prod" ? "REGIONAL" : "ZONAL"
    disk_type         = "PD_SSD"
    disk_size         = 20
    disk_autoresize   = true
    user_labels       = local.labels

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = var.environment == "prod"
    }

    ip_configuration {
      ipv4_enabled                                  = false
      private_network                               = google_compute_network.main.id
      enable_private_path_for_google_cloud_services = true
    }

    database_flags {
      name  = "cloudsql.enable_pgaudit"
      value = "on"
    }
  }

  depends_on = [google_service_networking_connection.private_services]
}

resource "google_sql_database" "app" {
  name     = "pipeline"
  instance = google_sql_database_instance.main.name
}

resource "google_sql_user" "app" {
  name     = "pipeline"
  instance = google_sql_database_instance.main.name
  password = var.postgres_password
}

resource "google_secret_manager_secret" "runtime" {
  for_each = toset(["database-url", "session-secret-key", "openai-api-key"])

  secret_id = "${local.name}-${each.value}"
  replication {
    auto {}
  }
  labels = local.labels
}

resource "google_secret_manager_secret_version" "database_url" {
  secret      = google_secret_manager_secret.runtime["database-url"].id
  secret_data = "postgresql+psycopg://${google_sql_user.app.name}:${urlencode(var.postgres_password)}@/${google_sql_database.app.name}?host=/cloudsql/${google_sql_database_instance.main.connection_name}"
}

resource "google_secret_manager_secret_version" "session_secret_key" {
  secret      = google_secret_manager_secret.runtime["session-secret-key"].id
  secret_data = var.session_secret_key
}

resource "google_secret_manager_secret_version" "openai_api_key" {
  secret      = google_secret_manager_secret.runtime["openai-api-key"].id
  secret_data = var.openai_api_key
}

resource "google_project_iam_member" "runtime" {
  for_each = toset([
    "roles/aiplatform.user",
    "roles/cloudsql.client",
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
  ])

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_storage_bucket_iam_member" "runtime" {
  bucket = google_storage_bucket.artifacts.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_secret_manager_secret_iam_member" "runtime" {
  for_each = google_secret_manager_secret.runtime

  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_cloud_run_v2_service" "app" {
  name     = local.name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"
  labels   = local.labels

  template {
    service_account = google_service_account.runtime.email
    timeout         = "300s"

    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [google_sql_database_instance.main.connection_name]
      }
    }

    containers {
      image = var.container_image

      ports {
        container_port = 8000
      }

      resources {
        limits = { cpu = "1", memory = "1Gi" }
      }

      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }

      env {
        name  = "AUTH_BYPASS_LOCAL"
        value = "false"
      }
      env {
        name  = "APP_ENVIRONMENT"
        value = var.environment == "prod" ? "production" : var.environment
      }
      env {
        name  = "SESSION_COOKIE_SECURE"
        value = "true"
      }
      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "GCS_BUCKET"
        value = google_storage_bucket.artifacts.name
      }
      env {
        name  = "PIPELINE_EXECUTION_TIMEOUT_SECONDS"
        value = "240"
      }
      env {
        name  = "MAX_UPLOAD_SIZE_BYTES"
        value = "52428800"
      }
      env {
        name  = "CLERK_PUBLISHABLE_KEY"
        value = var.clerk_publishable_key
      }
      env {
        name  = "CLERK_ISSUER"
        value = var.clerk_issuer
      }
      env {
        name  = "CLERK_AUDIENCE"
        value = var.clerk_audience
      }
      env {
        name  = "CLERK_JWKS_URL"
        value = var.clerk_jwks_url
      }
      env {
        name  = "CLERK_SIGN_IN_URL"
        value = var.clerk_sign_in_url
      }
      env {
        name  = "CLERK_JWT_TEMPLATE"
        value = var.clerk_jwt_template
      }

      dynamic "env" {
        for_each = {
          DATABASE_URL       = google_secret_manager_secret.runtime["database-url"].secret_id
          SESSION_SECRET_KEY = google_secret_manager_secret.runtime["session-secret-key"].secret_id
          OPENAI_API_KEY     = google_secret_manager_secret.runtime["openai-api-key"].secret_id
        }
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value
              version = "latest"
            }
          }
        }
      }
    }
  }

  depends_on = [
    google_project_iam_member.runtime,
    google_secret_manager_secret_iam_member.runtime,
  ]
}
