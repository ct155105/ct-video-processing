PROJECT_ID ?= $(shell gcloud config get-value project)
REGION     ?= us-central1
BUCKET     ?= $(PROJECT_ID)-raw-video-ingest

# Point directly to uv binaries
UV         := uv
PYTHON     := uv run python
ADK        := uv run adk

.PHONY: setup auth infra-init infra-apply ingest dev-ui deploy-indexer deploy-storyboard

## 0. Initialize project and sync dependencies via uv
setup:
	$(UV) sync

auth:
	gcloud auth login
	gcloud auth application-default login
	gcloud config set project $(PROJECT_ID)

infra-init:
	cd infra && terraform init

infra-apply:
	cd infra && terraform apply -var="project_id=$(PROJECT_ID)" -var="region=$(REGION)"

## 3. Run Google Photos -> GCS Ingestion using uv run
ingest:
	$(PYTHON) ingest/photos_to_gcs.py \
		--project-id=$(PROJECT_ID) \
		--bucket-name=$(BUCKET) \
		$(if $(JOB_ID),--job-id="$(JOB_ID)",)

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