PROJECT_ID   ?= $(shell gcloud config get-value project)
REGION       ?= us-central1
BUCKET       ?= $(PROJECT_ID)-raw-video-ingest
STATE_BUCKET ?= $(PROJECT_ID)-tfstate

# Point directly to uv binaries
UV           := uv
PYTHON       := uv run python
ADK          := uv run adk

.PHONY: setup auth infra-bootstrap infra-init infra-apply ingest dev-ui deploy-indexer deploy-storyboard

## 0. Initialize project and sync dependencies via uv
setup:
	$(UV) sync

auth:
	gcloud auth login
	gcloud auth application-default login
	gcloud config set project $(PROJECT_ID)

## 2a. One-time: create the versioned bucket that holds Terraform remote state.
## Must exist before `infra-init` — Terraform cannot provision its own backend,
## so this bucket is intentionally not a resource in infra/main.tf.
## Safe to re-run: creation is skipped if the bucket already exists, while real
## errors (permissions, invalid name) still fail the target rather than being
## swallowed. Versioning is re-asserted every run so state stays recoverable.
infra-bootstrap:
	@gcloud storage buckets describe gs://$(STATE_BUCKET) >/dev/null 2>&1 \
		&& echo "State bucket gs://$(STATE_BUCKET) already exists; skipping create." \
		|| gcloud storage buckets create gs://$(STATE_BUCKET) \
			--project=$(PROJECT_ID) \
			--location=$(REGION) \
			--uniform-bucket-level-access \
			--public-access-prevention
	gcloud storage buckets update gs://$(STATE_BUCKET) --versioning

## 2b. Initialize Terraform against the GCS backend. The bucket is injected here
## because backend blocks cannot interpolate variables (no var.project_id).
infra-init:
	cd infra && terraform init -backend-config="bucket=$(STATE_BUCKET)"

infra-apply:
	cd infra && terraform apply -var="project_id=$(PROJECT_ID)" -var="region=$(REGION)"

## 3. Run Google Photos -> GCS Ingestion using uv run.
## JOB_ID is required by the script, so it is validated here rather than passed
## conditionally — that produced an argparse usage dump after a slow interpreter
## start. PROJECT_ID is checked too, since an unset gcloud config would otherwise
## fail much later against a malformed bucket name.
ingest:
	@if [ -z "$(strip $(JOB_ID))" ]; then \
		echo "make: *** JOB_ID is required."; \
		echo "    Usage: make ingest JOB_ID=caiman_weekend_2026"; \
		exit 1; \
	fi
	@if [ -z "$(strip $(PROJECT_ID))" ]; then \
		echo "make: *** PROJECT_ID is empty — no active gcloud project."; \
		echo "    Fix with: gcloud config set project <id>   (or: make ingest PROJECT_ID=<id> JOB_ID=...)"; \
		exit 1; \
	fi
	$(PYTHON) ingest/photos_to_gcs.py \
		--project-id=$(PROJECT_ID) \
		--bucket-name=$(BUCKET) \
		--job-id="$(JOB_ID)"

## 4. Run local ADK Web UI via uv
dev-ui:
	$(ADK) web agents/storyboard_agent

## 5. Deploy Agents to Agent Runtime
deploy-indexer:
	$(ADK) deploy agent_engine \
		--project=$(PROJECT_ID) \
		--region=$(REGION) \
		agents/indexer_agent

deploy-storyboard:
	$(ADK) deploy agent_engine \
		--project=$(PROJECT_ID) \
		--region=$(REGION) \
		agents/storyboard_agent