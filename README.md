# Agentic Video Storyboarder & Production Pipeline

An automated, multimodal AI pipeline that ingests raw, unstructured video footage from Google Cloud Storage (GCS), extracts time-coded scene metadata and transcripts, autonomously ideates narrative arcs, and generates production-ready A-Roll/B-Roll storyboards with generative AI video prompts for missing visual coverage.

Built with **Google Agent Development Kit (ADK)**, **Vertex AI (Gemini 2.5 Pro & Flash)**, and deployed on **Gemini Enterprise Agent Platform (Agent Runtime)**.

---

## 🎯 Dual-Purpose Vision
* **Creator / Family Vlogs ("The Traveling T's"):** Turn 30+ minutes of raw, shaky family footage (e.g., zoo trips, reptile encounters) into cohesive 2–3 minute YouTube scripts with exact editing timestamps and synthetic B-roll prompts (e.g., generating prehistoric Megalodon clips to pair with fossil footage).

---

## 🏗️ High-Level Architecture

The system supports both **Event-Driven Ingestion** (via GCS + Pub/Sub + Firestore Scatter-Gather) and **Batch Picker Ingestion** orchestrated via ADK on **Agent Runtime**.

```mermaid
flowchart TD
    subgraph Ingestion ["1. Ingestion Layer (Colab / Python CLI)"]
        GP[Google Photos Picker API<br/>Interactive video selection] -->|Stream =dv| Script[Ingest Script]
        Script -->|1. Init Batch Doc| FS[(Firestore<br/>State & Metadata DB)]
        Script -->|2. Upload Raw Video| GCS[(Cloud Storage<br/>gs://raw-video-ingest)]
    end

    subgraph Extraction ["2. Parallel Multimodal Extraction (Agent Runtime)"]
        GCS -->|Object Finalized| EA[Eventarc / PubSub]
        EA -->|Trigger per Video| Indexer["Video Indexer Agent (LlmAgent)<br/>Model: gemini-2.5-flash"]
        Indexer -->|Native gs:// URI Analysis| Indexer
        Indexer -->|Write Clip JSON + Atomic Increment| FS
    end

    subgraph Synthesis ["3. Narrative & Storyboard Synthesis (ADK Workflow on Agent Runtime)"]
        FS -->|completed + failed == total| Trigger[Pub/Sub: trigger-storyboard]
        Trigger --> Root["Root Pipeline (SequentialAgent)"]
        
        Root --> Ideator["Idea Generator (LlmAgent)<br/>Model: gemini-2.5-pro<br/>Proposes 3 Story Arcs from Transcripts"]
        
        Ideator --> WritersRoom["Writers' Room (LoopAgent)"]
        
        subgraph Loop ["Iterative Storyboard Refinement"]
            Director["Director Agent (LlmAgent)<br/>Maps A-Roll & B-Roll Timestamps"] --> Critic["Pacing & Hook Critic (LlmAgent)<br/>Validates Arc & Audio Quality"]
            Critic -->|Feedback via SessionState| Director
        end
        
        WritersRoom -->|loop_exit| GenAI["GenAI B-Roll Prompt Agent (LlmAgent)<br/>Generates Veo / Seedance Prompts for Visual Gaps"]
        GenAI --> Output[Final Markdown & JSON Edit Decision List]
    end

```

---

## 🤖 Agent Hierarchy (Google ADK)

The core intelligence is structured using ADK's deterministic workflow primitives combined with Gemini 2.5 reasoning agents:

### 1. `video_indexer_agent` (`LlmAgent` - `gemini-2.5-flash`)

* **Role:** Assistant Editor.
* **Input:** A single `gs://` video URI.
* **Task:** Performs native multimodal analysis without manual frame/audio splitting. Extracts speaker diarization, verbatim dialogue transcripts, visual scene descriptions, audio quality scores, and A-roll/B-roll classification mapped to `[MM:SS]` timestamps.
* **Tools:** `save_clip_metadata_to_firestore`, `increment_job_counter`.

### 2. `storyboard_pipeline` (`SequentialAgent`)

Triggered once all videos in a batch finish indexing. Executes the following sub-agents in order:

* **`idea_generator_agent` (`LlmAgent` - `gemini-2.5-pro`):**
* **Task:** Reads all extracted clip JSONs from Firestore/SessionState. Identifies high-energy moments, thematic clusters, and natural climaxes (e.g., "Uncle Jimmy and the Caiman" or "Prehistoric Fossils") instead of relying on a human-supplied prompt. Outputs the strongest narrative arc into `state['selected_arc']`.


* **`writers_room` (`LoopAgent` - max 3 iterations):**
* **`director_agent` (`LlmAgent` - `gemini-2.5-pro`):** Drafts a time-coded script pairing clean A-roll voiceovers with engaging B-roll visual cutaways. Handles audio edge cases (e.g., boosting quiet speakers or spotlighting shy subjects in B-roll).
* **`critic_agent` (`LlmAgent` - `gemini-2.5-flash`):** Evaluates the draft for hook strength in the first 5 seconds, pacing, runtime targets (2–3 mins), and visual coverage. Calls `exit_loop` when quality thresholds are met, or appends revision notes to `state['critic_feedback']`.


* **`vfx_prompt_agent` (`LlmAgent` - `gemini-2.5-flash`):**
* **Task:** Scans the finalized storyboard for `[GEN_AI_BROLL_NEEDED]` tags (where speakers mention concepts lacking visual footage, such as a living Megalodon) and writes optimized text-to-video prompts for Veo / Seedance.



---

## 💾 Data Schemas (Firestore)

### Collection: `processing_jobs/{job_id}`

Tracks Scatter-Gather synchronization across asynchronous video indexing runs.

```json
{
  "job_id": "caiman_weekend_2026",
  "album_id": "ALBUM_ID_FROM_GOOGLE_PHOTOS",
  "total_videos": 14,
  "completed_videos": 13,
  "failed_videos": 1,
  "status": "SYNTHESIZING",
  "created_at": "2026-10-23T22:00:00Z"
}

```

### Sub-collection: `processing_jobs/{job_id}/clips/{clip_id}`

Output generated by `video_indexer_agent`.

```json
{
  "clip_id": "VID_20261018_142210.mp4",
  "gcs_uri": "gs://raw-video-ingest/caiman_weekend_2026/VID_20261018_142210.mp4",
  "duration_seconds": 142,
  "segments": [
    {
      "start_time": "00:12",
      "end_time": "00:28",
      "clip_type": "A_ROLL",
      "visual_description": "Grayson holding a large fossilized Megalodon tooth up to the lens.",
      "transcript": "Look how big this tooth is! Uncle Jimmy said Megalodon could eat a whale.",
      "speakers": ["Grayson"],
      "audio_clarity_score": 0.92,
      "excitement_score": 0.95,
      "tags": ["fossil", "megalodon", "grayson"]
    }
  ]
}

```

---

## 📁 Repository Structure

```text
├── Makefile                      # Task entry points: setup, auth, infra-*, ingest, dev-ui, deploy-*
├── pyproject.toml                # uv project definition + declared dependencies
├── uv.lock                       # Fully resolved dependency graph (committed for reproducibility)
├── .python-version               # Interpreter version pinned for uv
├── .gitignore                    # Ignores secrets, .venv, Terraform state & provider binaries
├── main.py                       # Placeholder from `uv init`
├── ingest/
│   ├── photos_to_gcs.py          # CLI: Google Photos Picker -> GCS + Firestore job init
│   └── client_secret.json        # OAuth desktop client — gitignored, never commit
├── infra/
│   ├── main.tf                   # Terraform: GCS, Firestore, Pub/Sub, IAM + GCS state backend
│   └── .terraform.lock.hcl       # Provider version lock (committed)
└── agents/                       # NOT YET IMPLEMENTED — planned layout below
    ├── indexer_agent/            # Triggered per video upload
    │   ├── __init__.py
    │   ├── agent.py              # LlmAgent definition (gemini-2.5-flash)
    │   └── tools.py              # Firestore atomic transaction & DLQ handlers
    └── storyboard_agent/         # Triggered on batch completion
        ├── __init__.py
        ├── agent.py              # Root SequentialAgent, LoopAgent, and sub-agents
        ├── prompts.py            # System instructions for Ideator, Director, Critic, VFX
        └── tools.py              # Firestore clip fetcher & exit_loop tools
```

Terraform state is **not** stored in this repo — it lives in `gs://<project-id>-tfstate`
(versioned) via the GCS backend configured in `infra/main.tf`.

---

## 🚀 Getting Started

### 1. Prerequisites

* Python 3.12+ and [uv](https://docs.astral.sh/uv/)
* `gcloud` CLI, authenticated
* Terraform >= 1.5
* A GCP project with billing enabled

All `make` targets derive the project from `gcloud config get-value project`.
Override any of them inline, e.g. `make infra-apply PROJECT_ID=my-other-project`.

### 2. Install dependencies

Dependencies are declared in `pyproject.toml` and pinned in `uv.lock`:

```bash
make setup          # uv sync — builds .venv from the lockfile
```

To add a package, use `uv add <package>` (this updates `pyproject.toml` **and**
`uv.lock`). Avoid `uv pip install` for project dependencies — it installs into the
venv without declaring anything, so the next `uv sync` silently removes it.

### 3. Authenticate

```bash
make auth           # gcloud auth login + application-default login
```

### 4. Provision infrastructure

```bash
make infra-bootstrap   # ONE-TIME: create the versioned GCS bucket for Terraform state
make infra-init        # terraform init against the GCS backend
make infra-apply       # GCS bucket, Firestore, Pub/Sub topics + DLQ, service account, IAM
```

`infra-bootstrap` must run before `infra-init`. Terraform cannot create the bucket
that stores its own state, so that bucket is provisioned with `gcloud` and is
deliberately not a resource in `main.tf`.

### 5. Ingest video from Google Photos

Opens a Google Photos Picker session, then streams the selected videos to GCS and
initializes the Firestore job document. `JOB_ID` is required:

```bash
make ingest JOB_ID=caiman_weekend_2026
```

Requires an OAuth **Desktop** client at `ingest/client_secret.json` (gitignored).

### 6. Local Development & Debugging (ADK Web UI)

Inspect session state, event traces, and loop iterations in the ADK developer UI:

```bash
make dev-ui         # adk web agents/storyboard_agent
```

### 7. Deploy to Vertex AI Agent Engine

```bash
make deploy-indexer
make deploy-storyboard
```

Both wrap `adk deploy agent_engine`. Note that `agent_engine` is the subcommand
exposed by the installed ADK (2.13.0); earlier drafts of this README referenced
`agent_runtime`, which is not a valid subcommand.