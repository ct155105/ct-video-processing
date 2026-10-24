import argparse
import datetime
import io
import os
import random
import time
import webbrowser
from typing import Dict, List, Tuple

import requests
from google.api_core import exceptions as api_exceptions
from google.auth.transport.requests import Request
from google.cloud import firestore, storage
from google.cloud.storage import exceptions as storage_exceptions
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials

# Picker API requires the photospicker.mediaitems.readonly scope
SCOPES = ["https://www.googleapis.com/auth/photospicker.mediaitems.readonly"]
PICKER_API_BASE = "https://photospicker.googleapis.com/v1"

CHUNK_SIZE = 16 * 1024 * 1024  # upload chunk; must be a multiple of 256 KiB
SUB_READ_SIZE = 1024 * 1024    # read granularity, so progress ticks within a chunk
MAX_ATTEMPTS = 4
# Connect timeout, then read timeout: large videos stream slowly but should not hang forever.
REQUEST_TIMEOUT = (30, 300)
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


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


def select_videos_via_picker(creds: Credentials) -> Tuple[List[Dict], str]:
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


def describe_error(exc: Exception) -> str:
    """Short, log-friendly rendering of a transfer failure, including HTTP status."""
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        return f"HTTP {exc.response.status_code} from {exc.response.url.split('?')[0]}"
    if isinstance(exc, api_exceptions.GoogleAPICallError):
        return f"GCS {exc.code} {exc.__class__.__name__}"
    if isinstance(exc, storage_exceptions.InvalidResponse):
        status = getattr(getattr(exc, "response", None), "status_code", "?")
        return f"GCS InvalidResponse {status}"
    return f"{type(exc).__name__}: {exc}"


def is_retryable(exc: Exception) -> bool:
    """True for transient failures where re-running the whole transfer may succeed."""
    if isinstance(exc, requests.exceptions.HTTPError):
        resp = exc.response
        return resp is not None and resp.status_code in RETRYABLE_STATUS
    if isinstance(
        exc,
        (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ),
    ):
        return True
    # 5xx from GCS (InternalServerError, ServiceUnavailable, BadGateway, ...)
    if isinstance(exc, api_exceptions.ServerError):
        return True
    if isinstance(exc, storage_exceptions.InvalidResponse):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        return status in RETRYABLE_STATUS
    # A retryable 5xx mid-upload makes the client library call stream.seek() to resume.
    # The download is a non-seekable socket, so recovery surfaces as UnsupportedOperation
    # rather than the underlying 5xx. Restarting the transfer from byte 0 is the recovery.
    if isinstance(exc, io.UnsupportedOperation):
        return True
    return False


def _format_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024 or unit == "GiB":
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GiB"


class ProgressReader(io.RawIOBase):
    """Wraps a download stream to report throughput as bytes pass through.

    A resumable upload in progress is not visible from outside GCS — the object
    only appears once complete — so per-video progress has to be reported here,
    at the point where the bytes are actually read.
    """

    def __init__(self, stream, prefix: str, total: int = 0, interval: float = 5.0):
        self._stream = stream
        self._prefix = prefix
        self._total = total
        self._interval = interval
        self._read = 0
        self._start = time.monotonic()
        self._last_report = self._start

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def tell(self) -> int:
        return self._read

    def _account(self, n: int) -> None:
        self._read += n
        now = time.monotonic()
        if now - self._last_report >= self._interval:
            self._last_report = now
            self._emit(now)

    def read(self, size: int = -1) -> bytes:
        # The uploader asks for a whole chunk_size (16 MiB) at a time, which on a
        # slow link is minutes of silence. Pull it in smaller sub-reads so progress
        # ticks independently of the upload chunk size.
        parts: List[bytes] = []
        remaining = size if size is not None and size >= 0 else None
        while remaining is None or remaining > 0:
            want = SUB_READ_SIZE if remaining is None else min(SUB_READ_SIZE, remaining)
            chunk = self._stream.read(want)
            if not chunk:
                break
            parts.append(chunk)
            if remaining is not None:
                remaining -= len(chunk)
            self._account(len(chunk))
        return b"".join(parts)

    def _emit(self, now: float) -> None:
        elapsed = max(now - self._start, 1e-6)
        rate = self._read / elapsed
        done = _format_bytes(self._read)
        speed = f"{_format_bytes(rate)}/s"
        if self._total:
            pct = 100.0 * self._read / self._total
            eta = (self._total - self._read) / rate if rate > 0 else 0
            print(
                f"{self._prefix} {done}/{_format_bytes(self._total)} "
                f"({pct:.0f}%) at {speed}, ETA {eta:.0f}s",
                flush=True,
            )
        else:
            print(f"{self._prefix} {done} at {speed}", flush=True)

    def summary(self) -> str:
        elapsed = max(time.monotonic() - self._start, 1e-6)
        return f"{_format_bytes(self._read)} in {elapsed:.0f}s ({_format_bytes(self._read / elapsed)}/s)"


def _transfer_once(
    creds: Credentials,
    bucket: storage.Bucket,
    blob_path: str,
    download_url: str,
    mime_type: str,
    metadata: Dict,
    progress_prefix: str = "",
) -> str:
    """One full attempt: fresh download stream piped into a fresh GCS upload.

    Each attempt re-opens the download so the upload always begins at byte 0 —
    an in-flight resumable upload cannot be resumed from a non-seekable socket.
    Returns a one-line transfer summary.
    """
    blob = bucket.blob(blob_path)
    blob.metadata = metadata
    blob.chunk_size = CHUNK_SIZE

    # Picker API baseUrl requests require the OAuth Bearer token header
    headers = {"Authorization": f"Bearer {creds.token}"}
    with requests.get(
        download_url, headers=headers, stream=True, timeout=REQUEST_TIMEOUT
    ) as r:
        r.raise_for_status()
        # Content-Length lets us report a percentage; Photos does not always send it.
        try:
            total = int(r.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            total = 0
        reader = ProgressReader(r.raw, progress_prefix, total=total)
        blob.upload_from_file(reader, content_type=mime_type, size=total or None)
        return reader.summary()


def stream_video_to_gcs(
    creds: Credentials,
    bucket: storage.Bucket,
    job_id: str,
    video_item: Dict,
    index: int,
    total: int,
) -> None:
    """Streams a video from Google Photos baseUrl to GCS, retrying transient failures.

    Raises the final exception if every attempt fails.
    """
    media_file = video_item["mediaFile"]
    # Append '=dv' to download the high-quality transcoded MP4 stream
    download_url = f"{media_file['baseUrl']}=dv"
    filename = media_file.get("filename", f"{video_item['id']}.mp4")
    mime_type = media_file.get("mimeType", "video/mp4")

    # Namespace the object path by job_id
    blob_path = f"{job_id}/{filename}"
    # Attach job_id in GCS object metadata so the Indexer Agent knows which Firestore doc to update
    metadata = {
        "job_id": job_id,
        "media_item_id": video_item["id"],
        "create_time": video_item.get("createTime", ""),
    }

    print(f"[{index}/{total}] 🌊 Streaming {filename} -> gs://{bucket.name}/{blob_path} ...")

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            # Long picker waits and multi-GB transfers can outlive the access token.
            if not creds.valid and creds.refresh_token:
                creds.refresh(Request())

            summary = _transfer_once(
                creds,
                bucket,
                blob_path,
                download_url,
                mime_type,
                metadata,
                progress_prefix=f"[{index}/{total}]    ",
            )
            print(f"[{index}/{total}] ✅ Finished uploading {filename} — {summary}")
            return
        except Exception as exc:
            if attempt == MAX_ATTEMPTS or not is_retryable(exc):
                raise
            backoff = min(2 ** (attempt - 1), 30) + random.uniform(0, 1)
            print(
                f"[{index}/{total}] ⚠️  Attempt {attempt}/{MAX_ATTEMPTS} failed "
                f"({describe_error(exc)}); retrying in {backoff:.1f}s"
            )
            time.sleep(backoff)


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

    # 5. Stream each video from Google Photos directly into GCS.
    # One bad video must not abandon the rest of the batch, so failures are
    # collected and reported instead of propagating out of the loop.
    uploaded = 0
    failures: List[Dict] = []
    try:
        for idx, video in enumerate(videos, start=1):
            filename = video.get("mediaFile", {}).get("filename", video["id"])
            try:
                stream_video_to_gcs(creds, bucket, args.job_id, video, idx, len(videos))
                uploaded += 1
            except Exception as exc:
                failures.append({"filename": filename, "error": describe_error(exc)})
                print(f"[{idx}/{len(videos)}] ❌ Gave up on {filename}: {describe_error(exc)}")

        job_ref = db.collection("processing_jobs").document(args.job_id)

        if uploaded == 0:
            job_ref.update(
                {"status": "INGEST_FAILED", "total_videos": 0, "ingest_failures": failures}
            )
            print(f"\n❌ No videos uploaded; {len(failures)} failed. Job marked INGEST_FAILED.")
        else:
            # total_videos must match what actually reached GCS. Videos that never
            # uploaded will never trigger the Indexer, so keeping the original count
            # would stall the Scatter-Gather (completed + failed == total never true).
            update = {"status": "INDEXING", "total_videos": uploaded}
            if failures:
                update["ingest_failures"] = failures
            job_ref.update(update)

            print(f"\n🎉 {uploaded}/{len(videos)} videos streamed to gs://{args.bucket_name}/{args.job_id}/")
            if failures:
                print(f"⚠️  {len(failures)} video(s) failed and were excluded from total_videos:")
                for f in failures:
                    print(f"     - {f['filename']}: {f['error']}")
    finally:
        cleanup_picker_session(creds, session_id)

    if failures and uploaded == 0:
        raise SystemExit(1)


if __name__ == "__main__":
    main()