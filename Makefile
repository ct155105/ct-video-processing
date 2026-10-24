PROJECT_ID   ?= $(shell gcloud config get-value project)
REGION       ?= us-central1
STATE_BUCKET ?= $(PROJECT_ID)-tfstate

.PHONY: auth infra-bootstrap infra-init infra-apply dev-ui deploy-indexer deploy-storyboard

## 1. Authenticate local environment
auth:
	gcloud auth login
	gcloud auth application-default login
	gcloud config set project $(PROJECT_ID)

## 2a. One-time: create the versioned bucket that holds Terraform state.
## Must exist before `infra-init` — Terraform cannot provision its own backend.
## Safe to re-run: a pre-existing bucket is reported and left untouched.
infra-bootstrap:
	gcloud storage buckets create gs://$(STATE_BUCKET) \
		--project=$(PROJECT_ID) \
		--location=$(REGION) \
		--uniform-bucket-level-access \
		--public-access-prevention || true
	gcloud storage buckets update gs://$(STATE_BUCKET) --versioning

## 2b. Initialize Terraform against the GCS backend.
## The bucket is injected here because backend blocks cannot use variables.
infra-init:
	cd infra && terraform init -backend-config="bucket=$(STATE_BUCKET)"

infra-apply:
	cd infra && terraform apply -var="project_id=$(PROJECT_ID)" -var="region=$(REGION)"

## 3. Run local ADK Web UI for debugging prompts & session state
dev-ui:
	adk web agents/storyboard_agent

## 4. Deploy Agents to Agent Runtime
deploy-indexer:
	adk deploy agent_engine \
		--project=$(PROJECT_ID) \
		--region=$(REGION) \
		agents/indexer_agent

deploy-storyboard:
	adk deploy agent_engine \
		--project=$(PROJECT_ID) \
		--region=$(REGION) \
		agents/storyboard_agent