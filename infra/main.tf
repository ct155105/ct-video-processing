terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }

  # Remote state in GCS: survives loss of this machine, supports locking, and
  # keeps state out of git. The bucket is versioned so a corrupted or truncated
  # state can be rolled back to a prior generation.
  #
  # Partial configuration — `bucket` is passed at init time rather than hardcoded,
  # because backend blocks cannot interpolate variables (no var.project_id here).
  # Use `make infra-init`, which supplies it; a bare `terraform init` will prompt.
  #
  # The state bucket is deliberately NOT managed by this config: Terraform cannot
  # create the bucket it stores its own state in. See `make infra-bootstrap`.
  backend "gcs" {
    prefix = "infra/state"
  }
}

variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "region" {
  description = "Default GCP Region"
  type        = string
  default     = "us-central1"
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# 1. Enable Required GCP APIs
resource "google_project_service" "apis" {
  for_each = toset([
    "aiplatform.googleapis.com",
    "firestore.googleapis.com",
    "pubsub.googleapis.com",
    "storage.googleapis.com",
    "run.googleapis.com",
    "cloudbuild.googleapis.com",
    "photospicker.googleapis.com"
  ])
  service            = each.key
  disable_on_destroy = false
}

# 2. Cloud Storage Bucket for Raw Video Ingestion
resource "google_storage_bucket" "raw_videos" {
  name                        = "${var.project_id}-raw-video-ingest"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true # Set to false in enterprise prod

  depends_on = [google_project_service.apis]
  lifecycle_rule {
    condition {
      age = 30
    }
    action {
      type = "Delete"
    }
  }
}

# 3. Firestore Database (Native Mode) for Scatter-Gather & Clip Metadata
resource "google_firestore_database" "metadata_db" {
  project     = var.project_id
  name        = "(default)"
  location_id = var.region
  type        = "FIRESTORE_NATIVE"

  depends_on = [google_project_service.apis]
}

# 4. Pub/Sub Topics (Ingest, Dead Letter Queue, and Storyboard Trigger)
resource "google_pubsub_topic" "video_ingest" {
  name       = "video-ingest-events"
  depends_on = [google_project_service.apis]
}

resource "google_pubsub_topic" "video_ingest_dlq" {
  name       = "video-ingest-dlq"
  depends_on = [google_project_service.apis]
}

resource "google_pubsub_topic" "trigger_storyboard" {
  name       = "trigger-storyboard-workflow"
  depends_on = [google_project_service.apis]
}

# 5. Wire GCS Object Finalized Directly to Pub/Sub
data "google_storage_project_service_account" "gcs_account" {
  depends_on = [google_project_service.apis]
}

resource "google_pubsub_topic_iam_member" "gcs_publisher" {
  topic  = google_pubsub_topic.video_ingest.id
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${data.google_storage_project_service_account.gcs_account.email_address}"
}

resource "google_storage_notification" "video_upload_notification" {
  bucket         = google_storage_bucket.raw_videos.name
  payload_format = "JSON_API_V1"
  topic          = google_pubsub_topic.video_ingest.id
  event_types    = ["OBJECT_FINALIZE"]

  depends_on = [google_pubsub_topic_iam_member.gcs_publisher]
}

# 6. Subscription with 600s Ack Deadline & DLQ for Heavy Multimodal Indexing
resource "google_pubsub_subscription" "video_ingest_sub" {
  name                 = "video-ingest-indexer-sub"
  topic                = google_pubsub_topic.video_ingest.name
  ack_deadline_seconds = 600 # 10 minutes for Gemini multimodal processing

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.video_ingest_dlq.id
    max_delivery_attempts = 5
  }

  retry_policy {
    minimum_backoff = "15s"
    maximum_backoff = "300s"
  }
}

# 7. Dedicated Service Account for the ADK Agents
resource "google_service_account" "agent_sa" {
  account_id   = "video-storyboard-agent-sa"
  display_name = "Video Storyboard ADK Agent Service Account"
}

resource "google_project_iam_member" "agent_permissions" {
  for_each = toset([
    "roles/aiplatform.user",
    "roles/datastore.user",
    "roles/storage.objectViewer",
    "roles/pubsub.publisher",
    "roles/pubsub.subscriber"
  ])
  project = var.project_id
  role    = each.key
  member  = "serviceAccount:${google_service_account.agent_sa.email}"
}

output "raw_video_bucket" {
  value = google_storage_bucket.raw_videos.name
}

output "agent_service_account" {
  value = google_service_account.agent_sa.email
}