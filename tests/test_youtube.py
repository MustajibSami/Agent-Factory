"""YouTube posting, with a fake YouTube so no network or Google account is needed."""
import pytest

from factory import config
from factory.publish.youtube import UploadError, YouTubeUploader, clean
from factory.runner import Factory


class FakeYouTube:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def upload(self, video, title, description, tags):
        self.calls.append({"video": video, "title": title, "description": description, "tags": tags})
        if self.fail:
            raise UploadError(self.fail)
        return {"video_id": "abc123", "url": "https://youtube.com/shorts/abc123", "privacy": "private"}


@pytest.fixture
def fac(tmp_path, monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    cfg = config.load(tmp_path)
    for k, v in {"factory.pace_seconds": 0, "llm.mode": "mock", "publishing.youtube.live": True,
                 "video.width": 180, "video.height": 320, "video.fps": 8, "video.seconds_per_line": 1.0}.items():
        cfg.set(k, v)
    return Factory(cfg)


def ready_video(fac):
    """Push one trend through to a rendered, reviewed video waiting for approval."""
    fac.db.create_job("shorts", "trend", "Blender donut but it is a real product render",
                      {"platform": "manual", "views": 500000})
    for room in ("trend_finder", "control_room", "video_maker", "review_room"):
        assert fac.rooms[room].step()
    job = fac.db.jobs()[0]
    assert job["status"] == "awaiting_you"
    return job


def test_nothing_uploads_before_approval(fac):
    fac.ctx.youtube = FakeYouTube()
    ready_video(fac)
    assert fac.rooms["acc_manager"].step() is False
    assert fac.ctx.youtube.calls == []


def test_approved_video_uploads_once(fac):
    fac.ctx.youtube = yt = FakeYouTube()
    job = ready_video(fac)
    fac.db.finish(job["id"], "ready")
    assert fac.rooms["acc_manager"].step()
    done = fac.db.get_job(job["id"])
    assert done["status"] == "posted"
    assert done["data"]["posts"]["youtube"]["url"] == "https://youtube.com/shorts/abc123"
    assert len(yt.calls) == 1
    call = yt.calls[0]
    assert call["title"].endswith("#Shorts") and len(call["title"]) <= 100
    assert call["video"].exists()
    assert (fac.cfg.var / "outbox/posts" / f"job_{job['id']}_tiktok.json").exists()   # TikTok stays manual
    messages = [e["message"] for e in fac.db.events_after(job_id=job["id"])]
    assert any("uploaded to YouTube as private" in m for m in messages)


def test_failed_upload_pauses_room_and_keeps_card(fac):
    fac.ctx.youtube = FakeYouTube(fail="YouTube's daily upload limit is reached")
    job = ready_video(fac)
    fac.db.finish(job["id"], "ready")
    assert fac.rooms["acc_manager"].step() is False
    assert fac.db.get_job(job["id"])["status"] == "ready"        # the card is not lost
    assert fac.db.paused("acc_manager")
    errors = [e["message"] for e in fac.db.events_after() if e["kind"] == "error"]
    assert "daily upload limit" in errors[-1]


def test_card_already_uploaded_is_not_uploaded_again(fac):
    fac.ctx.youtube = yt = FakeYouTube()
    job = ready_video(fac)
    fac.db.finish(job["id"], "ready")
    fac.db.patch(job["id"], {"posts": {"youtube": {"video_id": "old", "url": "u", "privacy": "private"}}})
    assert fac.rooms["acc_manager"].step()
    assert yt.calls == []                                         # already uploaded earlier
    assert fac.db.get_job(job["id"])["status"] == "posted"


def test_not_signed_in_gives_clear_instruction(fac):
    with pytest.raises(UploadError, match="youtube-login"):
        YouTubeUploader(fac.cfg).upload(fac.cfg.var / "x.mp4", "t", "d", [])


def test_clean_strips_angle_brackets():
    assert clean("<b>Hi</b> there", 100) == "bHi/b there"
    assert len(clean("x" * 200, 100)) == 100


def _real_uploader_with(fac, responses, monkeypatch):
    """The real uploader, talking to the Google library's HTTP simulator instead of the internet."""
    from googleapiclient.discovery import build
    from googleapiclient.http import HttpMockSequence
    http = HttpMockSequence(responses)
    up = YouTubeUploader(fac.cfg)
    monkeypatch.setattr(up, "_client", lambda: build("youtube", "v3", http=http, static_discovery=True))
    video = fac.cfg.var / "v.mp4"
    video.write_bytes(b"0" * 2048)
    return up, video, http


def test_real_uploader_sends_private_short(fac, monkeypatch):
    import json
    up, video, http = _real_uploader_with(fac, [
        ({"status": "200", "location": "https://upload.example/session"}, ""),
        ({"status": "200"}, json.dumps({"id": "xyz789"})),
    ], monkeypatch)
    sent = []
    orig = http.request
    def spy(uri, method="GET", body=None, headers=None, **kw):
        sent.append((method, uri, body))
        return orig(uri, method, body, headers, **kw)
    http.request = spy
    result = up.upload(video, "<Spin> it #Shorts", "desc", ["#3d", "#blender"])
    assert result == {"video_id": "xyz789", "url": "https://youtube.com/shorts/xyz789", "privacy": "private"}
    method, uri, body = sent[0]
    assert method == "POST" and "uploadType=resumable" in uri and "part=snippet%2Cstatus" in uri
    meta = json.loads(body)
    assert meta["status"] == {"privacyStatus": "private", "selfDeclaredMadeForKids": False}
    assert meta["snippet"]["title"] == "Spin it #Shorts" and meta["snippet"]["tags"] == ["3d", "blender"]


def test_real_uploader_explains_quota_error(fac, monkeypatch):
    import json
    err = {"error": {"code": 403, "message": "quota", "errors": [{"reason": "quotaExceeded", "message": "quota"}]}}
    up, video, _ = _real_uploader_with(fac, [({"status": "403"}, json.dumps(err))], monkeypatch)
    with pytest.raises(UploadError, match="daily upload limit"):
        up.upload(video, "t", "d", [])
