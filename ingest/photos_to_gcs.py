import argparse
import datetime
import os
import time
import webbrowser
from typing import Dict, List

import requests
from google.auth.transport.requests import Request
from google.cloud import firestore, storage
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials

# Picker API requires the photospicker.mediaitems.readonly scope
SCOPES = ["https://www.googleapis.com/auth/photospicker.mediaitems.readonly"]
PICKER_API_BASE = "https://photospicker.googleapis.com/v1"


def get_photos_credentials(client_secret_path: str, token_path: str) -> Credentials:
    """Authenticates the user via OAuth 2.0 and caches the token locally."""
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, SCOPES)
            creds = flow.run_local_server(port=8080)
        with open(token_path, "w") as token_file:
            token_file.write(creds.to_json())

    return creds


def select_videos_via_picker(creds: Credentials) -> List[Dict]:
    """Creates a Picker session, waits for user selection, and returns picked VIDEO items."""
    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json",
    }

    # 1. Create a PickingSession
    resp = requests.post(f"{PICKER_API_BASE}/sessions", headers=headers, json={})
    resp.raise_for_status()
    session = resp.json()

    session_id = session["id"]
    # Appending /autoclose automatically closes the browser tab once you click "Done"
    picker_uri = f"{session['pickerUri']}/autoclose"

    print("\n" + "=" * 70)
    print("📸 Launching Google Photos Picker in your browser...")
    print(f"If it doesn't open automatically, click here:\n{picker_uri}")
    print("=" * 70 + "\n")
    webbrowser.open(picker_uri)

    # 2. Poll the session until mediaItemsSet is True
    poll_interval = int(
        session.get("pollingConfig", {}).get("pollInterval", "5s").rstrip("s") or 5
    )
    print("Waiting for you to select your album/videos and click 'Done'...")

    while True:
        time.sleep(poll_interval)
        status_resp = requests.get(
            f"{PICKER_API_BASE}/sessions/{session_id}", headers=headers
        )
        status_resp.raise_for_status()
        status_data = status_resp.json()

        if status_data.get("mediaItemsSet"):
            print("✅ Selection complete! Fetching chosen media metadata...")
            break

    # 3. List all picked media items (handling pagination)
    videos = []
    page_token = None

    while True:
        params = {"sessionId": session_id, "pageSize": 100}
        if page_token:
            params["pageToken"] = page_token

        list_resp = requests.get(
            f"{PICKER_API_BASE}/mediaItems", headers=headers, params=params
        )
        list_resp.raise_for_status()
        data = list_resp.json()

        for item in data.get("mediaItems", []):
            if item.get("type") == "VIDEO":
                proc_status = (
                    item.get("mediaFile", {})
                    .get("mediaFileMetadata", {})
                    .get("videoMetadata", {})
                    .get("processingStatus", "READY")
                )
                if proc_status == "READY":
                    videos.append(item)
                else:
                    fname = item.get("mediaFile", {}).get("filename", item["id"])
                    print(f"⚠️ Skipping {fname} (status is {proc_status}, not READY)")

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return videos, session_id


def init_firestore_job(db: firestore.Client, job_id: str, total_videos: int) -> None:
    """Initializes the Scatter-Gather synchronization document in Firestore."""
    job_ref = db.collection("processing_jobs").document(job_id)
    job_ref.set(
        {
            "job_id": job_id,
            "total_videos": total_videos,
            "completed_videos": 0,
            "failed_videos": 0,
            "status": "INGESTING",
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
    )
    print(f"📝 Initialized Firestore job 'processing_jobs/{job_id}' with total_videos={total_videos}")


def stream_video_to_gcs(
    creds: Credentials,
    bucket: storage.Bucket,
    job_id: str,
    video_item: Dict,
    index: int,
    total: int,
) -> None:
    """Streams a video directly from Google Photos baseUrl to GCS without saving to disk."""
    media_file = video_item["mediaFile"]
    # Append '=dv' to download the high-quality transcoded MP4 stream
    download_url = f"{media_file['baseUrl']}=dv"
    filename = media_file.get("filename", f"{video_item['id']}.mp4")
    mime_type = media_file.get("mimeType", "video/mp4")

    # Namespace the object path by job_id
    blob_path = f"{job_id}/{filename}"
    blob = bucket.blob(blob_path)

    # Attach job_id in GCS object metadata so the Indexer Agent knows which Firestore doc to update
    blob.metadata = {
        "job_id": job_id,
        "media_item_id": video_item["id"],
        "create_time": video_item.get("createTime", ""),
    }

    # Picker API baseUrl requests require the OAuth Bearer token header
    headers = {"Authorization": f"Bearer {creds.token}"}

    print(f"[{index}/{total}] 🌊 Streaming {filename} -> gs://{bucket.name}/{blob_path} ...")
    with requests.get(download_url, headers=headers, stream=True) as r:
        r.raise_for_status()
        # Stream directly in 16 MB chunks to GCS
        blob.chunk_size = 16 * 1024 * 1024
        blob.upload_from_file(r.raw, content_type=mime_type)

    print(f"[{index}/{total}] ✅ Finished uploading {filename}")


def cleanup_picker_session(creds: Credentials, session_id: str) -> None:
    """Deletes the Picker session after all video bytes have been transferred."""
    headers = {"Authorization": f"Bearer {creds.token}"}
    requests.delete(f"{PICKER_API_BASE}/sessions/{session_id}", headers=headers)


def main():
    parser = argparse.ArgumentParser(description="Stream Google Photos videos to GCS")
    parser.add_argument("--project-id", required=True, help="GCP Project ID")
    parser.add_argument("--bucket-name", required=True, help="Target GCS Bucket Name")
    parser.add_argument("--job-id", required=True, help="Unique Job ID (e.g., caiman_weekend_2026)")
    parser.add_argument(
        "--client-secret",
        default="ingest/client_secret.json",
        help="Path to OAuth Desktop client_secret.json",
    )
    parser.add_argument(
        "--token-path",
        default="ingest/token.json",
        help="Path to cached OAuth token.json",
    )
    args = parser.parse_args()

    # 1. Authenticate with Google Photos Picker API
    creds = get_photos_credentials(args.client_secret, args.token_path)

    # 2. Launch interactive Picker session & collect selected videos
    videos, session_id = select_videos_via_picker(creds)
    if not videos:
        print("No ready videos were selected. Exiting.")
        cleanup_picker_session(creds, session_id)
        return

    print(f"🎬 Found {len(videos)} ready video(s) to ingest.")

    # 3. Initialize GCP clients (uses your `gcloud auth application-default login` credentials)
    db = firestore.Client(project=args.project_id)
    storage_client = storage.Client(project=args.project_id)
    bucket = storage_client.bucket(args.bucket_name)

    # 4. Create the Firestore Scatter-Gather Job BEFORE uploading to GCS
    init_firestore_job(db, args.job_id, len(videos))

    # 5. Stream each video from Google Photos directly into GCS
    try:
        for idx, video in enumerate(videos, start=1):
            # Refresh token if needed during long 4K batch transfers
            if not creds.valid and creds.refresh_token:
                creds.refresh(Request())
            stream_video_to_gcs(creds, bucket, args.job_id, video, idx, len(videos))

        # Mark ingestion complete
        db.collection("processing_jobs").document(args.job_id).update(
            {"status": "INDEXING"}
        )
        print(f"\n🎉 All {len(videos)} videos streamed to gs://{args.bucket_name}/{args.job_id}/")
    finally:
        cleanup_picker_session(creds, session_id)


if __name__ == "__main__":
    main()