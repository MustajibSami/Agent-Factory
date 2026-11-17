"""Uploads approved Shorts to YouTube through the official YouTube Data API.

One-time setup (see README): create an OAuth client in Google Cloud, save it
as secrets/youtube_client.json, then run `python -m factory youtube-login`.
"""
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


class UploadError(Exception):
    """The upload did not happen. The message says what you need to do."""


def _paths(cfg):
    return (cfg.root / cfg.get("publishing.youtube.client_secrets", "secrets/youtube_client.json"),
            cfg.root / cfg.get("publishing.youtube.token", "secrets/youtube_token.json"))


def login(cfg) -> Path:
    """Open the browser once so you can let the factory upload to your channel."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    secrets, token = _paths(cfg)
    if not secrets.exists():
        raise UploadError(f"OAuth client file not found at {secrets}. See 'Posting to YouTube' in the README.")
    creds = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES).run_local_server(port=0)
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(creds.to_json())
    return token


class YouTubeUploader:
    def __init__(self, cfg):
        self.cfg = cfg

    def _client(self):
        from google.auth.exceptions import RefreshError, TransportError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        _, token = _paths(self.cfg)
        if not token.exists():
            raise UploadError("not signed in to YouTube: run  python -m factory youtube-login")
        creds = Credentials.from_authorized_user_file(str(token), SCOPES)
        if not creds.valid:
            try:
                creds.refresh(Request())
            except RefreshError as e:
                raise UploadError("YouTube sign-in expired or was revoked: run  python -m factory youtube-login") from e
            except TransportError as e:
                raise UploadError(f"could not reach Google to refresh the sign-in: {e}") from e
            token.write_text(creds.to_json())
        return build("youtube", "v3", credentials=creds, cache_discovery=False)

    def upload(self, video: Path, title: str, description: str, tags: list[str]) -> dict:
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload
        privacy = self.cfg.get("publishing.youtube.privacy", "private")
        body = {
            "snippet": {"title": clean(title, 100), "description": clean(description, 4900),
                        "tags": [t.lstrip("#") for t in tags][:15],
                        "categoryId": str(self.cfg.get("publishing.youtube.category_id", "22"))},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
        }
        try:
            request = self._client().videos().insert(
                part="snippet,status", body=body,
                media_body=MediaFileUpload(str(video), mimetype="video/mp4", chunksize=-1, resumable=True))
            response = None
            while response is None:
                _, response = request.next_chunk()
        except HttpError as e:
            reason = _reason(e)
            if reason in ("quotaExceeded", "uploadLimitExceeded", "rateLimitExceeded"):
                raise UploadError(f"YouTube's daily upload limit is reached ({reason}). Resume this room tomorrow.") from e
            if e.resp.status in (401, 403):
                raise UploadError(f"YouTube refused the upload ({reason or e.resp.status}). "
                                  "Check the channel and run  python -m factory youtube-login") from e
            raise UploadError(f"YouTube upload failed ({e.resp.status} {reason})") from e
        except OSError as e:
            raise UploadError(f"could not reach YouTube: {e}") from e
        vid = response["id"]
        return {"video_id": vid, "url": f"https://youtube.com/shorts/{vid}", "privacy": privacy}


def clean(text: str, limit: int) -> str:
    """YouTube rejects < and > in titles and descriptions."""
    return text.replace("<", "").replace(">", "").strip()[:limit]


def _reason(e) -> str:
    try:
        return e.error_details[0].get("reason", "")
    except (AttributeError, IndexError, TypeError):
        return ""
