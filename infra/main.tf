locals {
  name = "${var.project_name}-${var.environment}"
  labels = {
    application = var.project_name
    environment = var.environment
    managed-by  = "terraform"
  }
  required_apis = toset([
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudresourcemanager.googleapis.com",
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

resource "google_service_account" "github_deployer" {
  count = var.github_repository == "" ? 0 : 1

  account_id   = "${var.project_name}-${var.environment}-gh"
  display_name = "${local.name} GitHub Actions deployer"
}

resource "google_iam_workload_identity_pool" "github" {
  count = var.github_repository == "" ? 0 : 1

  workload_identity_pool_id = "${local.name}-github"
  display_name              = "${local.name} GitHub Actions"
  description               = "GitHub Actions identities permitted to deploy ${local.name}"
}

resource "google_iam_workload_identity_pool_provider" "github" {
  count = var.github_repository == "" ? 0 : 1

  workload_identity_pool_id          = google_iam_workload_identity_pool.github[0].workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  display_name                       = "GitHub Actions OIDC"
  attribute_condition                = "assertion.repository == '${var.github_repository}'"
  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }
  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account_iam_member" "github_deployer" {
  count = var.github_repository == "" ? 0 : 1

  service_account_id = google_service_account.github_deployer[0].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github[0].name}/attribute.repository/${var.github_repository}"
}

resource "google_project_iam_member" "github_deployer" {
  for_each = var.github_repository == "" ? toset([]) : toset([
    "roles/artifactregistry.admin",
    "roles/cloudbuild.builds.editor",
    "roles/compute.networkAdmin",
    "roles/iam.serviceAccountAdmin",
    "roles/iam.serviceAccountUser",
    "roles/iam.workloadIdentityPoolAdmin",
    "roles/resourcemanager.projectIamAdmin",
    "roles/run.admin",
    "roles/secretmanager.admin",
    "roles/servicenetworking.networksAdmin",
    "roles/serviceusage.serviceUsageAdmin",
    "roles/cloudsql.admin",
    "roles/storage.admin",
  ])

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.github_deployer[0].email}"
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
  name                        = var.artifact_bucket_name != "" ? var.artifact_bucket_name : "${var.project_id}-${local.name}-artifacts"
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

resource "google_storage_bucket" "terraform_state" {
  name                        = var.terraform_state_bucket_name
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false
  labels                      = local.labels

  versioning {
    enabled = true
  }
}

resource "google_compute_network" "main" {
  name                    = "${local.name}-vpc"
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "serverless" {
  name                     = "${local.name}-serverless"
  region                   = var.region
  network                  = google_compute_network.main.id
  ip_cidr_range            = var.serverless_vpc_subnet_cidr
  private_ip_google_access = true
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
    edition           = "ENTERPRISE"
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
  for_each = var.secret_ids

  secret_id = each.value
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

resource "google_secret_manager_secret_version" "byok_encryption_key" {
  secret      = google_secret_manager_secret.runtime["byok-encryption-key"].id
  secret_data = var.byok_encryption_key
}

resource "google_secret_manager_secret_version" "clerk_secret_key" {
  secret      = google_secret_manager_secret.runtime["clerk-secret-key"].id
  secret_data = var.clerk_secret_key
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

data "google_project" "current" {}

resource "google_artifact_registry_repository_iam_member" "cloud_build" {
  location   = google_artifact_registry_repository.app.location
  repository = google_artifact_registry_repository.app.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${data.google_project.current.number}-compute@developer.gserviceaccount.com"
}

resource "google_artifact_registry_repository_iam_member" "cloud_run" {
  location   = google_artifact_registry_repository.app.location
  repository = google_artifact_registry_repository.app.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:service-${data.google_project.current.number}@serverless-robot-prod.iam.gserviceaccount.com"
}

resource "google_project_iam_member" "cloud_build" {
  project = var.project_id
  role    = "roles/cloudbuild.builds.builder"
  member  = "serviceAccount:${data.google_project.current.number}-compute@developer.gserviceaccount.com"
}

resource "google_project_iam_member" "cloud_run_vpc_access" {
  project = var.project_id
  role    = "roles/compute.networkUser"
  member  = "serviceAccount:service-${data.google_project.current.number}@serverless-robot-prod.iam.gserviceaccount.com"
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
    labels = merge(
      local.labels,
      var.deployment_nonce == "" ? {} : { "deployment-nonce" = var.deployment_nonce },
    )

    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    vpc_access {
      egress = "PRIVATE_RANGES_ONLY"
      network_interfaces {
        network    = google_compute_network.main.id
        subnetwork = google_compute_subnetwork.serverless.id
      }
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
        name  = "MAX_SOURCE_FILES"
        value = "10"
      }
      env {
        name  = "MAX_TOTAL_UPLOAD_SIZE_BYTES"
        value = "104857600"
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
      env {
        name  = "BOOTSTRAP_ADMIN_EMAILS"
        value = join(",", var.bootstrap_admin_emails)
      }

      dynamic "env" {
        for_each = {
          DATABASE_URL        = google_secret_manager_secret.runtime["database-url"].secret_id
          SESSION_SECRET_KEY  = google_secret_manager_secret.runtime["session-secret-key"].secret_id
          BYOK_ENCRYPTION_KEY = google_secret_manager_secret.runtime["byok-encryption-key"].secret_id
          CLERK_SECRET_KEY    = google_secret_manager_secret.runtime["clerk-secret-key"].secret_id
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
    google_project_iam_member.cloud_run_vpc_access,
    google_artifact_registry_repository_iam_member.cloud_run,
  ]
}

resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  project  = var.project_id
  location = google_cloud_run_v2_service.app.location
  name     = google_cloud_run_v2_service.app.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
