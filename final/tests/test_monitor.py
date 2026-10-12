import json
from pathlib import Path

from PIL import Image, ImageDraw

from baselines import acknowledge, connect, get_baseline
from capture import CaptureError, CaptureResult, build_options, capture, extract_fields, redact_secrets, with_body_fallback
from compare import compare_images
from config import Settings, Target, load_settings
from explain import Explanation, jpeg_bytes_for_model, parse_explanation
from gate import crosses
from monitor import run_once
from normalize import normalize_files
from notify import alert_payload, write_alert
from storage_lookup import extract_value, lookup_change, needs_html, text_for_selector


def _jpeg(path: Path, size=(80, 60), color=(240, 240, 240), box=None, box_color=(20, 20, 20)):
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", size, color)
    if box:
        ImageDraw.Draw(image).rectangle(box, fill=box_color)
    image.save(path, format="JPEG", quality=95)
    return path


def _settings(tmp_path: Path, **overrides) -> Settings:
    data = dict(
        targets=(
            Target(
                url="https://news.ycombinator.com",
                devices=("desktop",),
                css_click_selector="",
                extract_selector="",
            ),
        ),
        shots_dir=tmp_path / "shots",
        heatmaps_dir=tmp_path / "heatmaps",
        alerts_dir=tmp_path / "alerts",
        db_path=tmp_path / "monitor.sqlite",
        rotate_after_n=5,
        compare_width=80,
        ssim_delta=0.04,
        area_delta=0.03,
        page_wait=2000,
        scroll_interval=10,
        timeout=120,
    )
    data.update(overrides)
    return Settings(**data)


def test_load_settings_resolves_next_to_the_toml(tmp_path):
    path = tmp_path / "targets.toml"
    path.write_text(
        'ssim_delta = 0.1\n[[targets]]\nurl = "https://example.com"\ndevices = ["mobile"]\n',
        encoding="utf-8",
    )
    settings = load_settings(path)
    assert settings.targets[0].devices == ("mobile",)
    assert settings.shots_dir == tmp_path / "shots"
    assert settings.ssim_delta == 0.1


def test_click_selector_fallback_and_options_omit_country():
    assert with_body_fallback("") == ""
    assert with_body_fallback(".mw-cookiebanner-dismiss") == ".mw-cookiebanner-dismiss,body"
    assert with_body_fallback("#a,body|.b") == "#a,body|.b,body"
    options = build_options("mobile", 2000, 10, ".banner")
    assert options["screenshot"] == "true"
    assert options["store"] == "true"
    assert options["format"] == "json"
    assert options["scroll"] == "true"
    assert options["scroll_interval"] == "10"
    assert options["page_wait"] == "2000"
    assert options["device"] == "mobile"
    assert options["css_click_selector"] == ".banner,body"
    assert "country" not in options
    assert "country" not in build_options("desktop", 2000, 10, "")


def test_rid_is_taken_from_the_screenshot_url_when_the_body_omits_it():
    response = {
        "status_code": 200,
        "headers": {"cb_status": "200"},
        "body": json.dumps(
            {
                "cb_status": 200,
                "url": "https://news.ycombinator.com",
                "screenshot_url": "https://api.crawlbase.com/storage?rid=abc123rid&token=SECRETVALUE",
                "body": "<html><h1>Hacker News</h1></html>",
            }
        ).encode(),
    }
    fields = extract_fields(response)
    assert fields.rid == "abc123rid"
    assert fields.storage_url_present
    assert fields.page_html.startswith("<html>")
    assert "SECRETVALUE" not in fields.rid
    response = {
        "status_code": 200,
        "headers": {"cb_status": "200", "url": "https://example.com", "original_status": "200"},
        "body": json.dumps(
            {
                "cb_status": 200,
                "url": "https://example.com",
                "screenshot_url": "https://shots.example/a.jpg",
                "rid": "rid-1",
                "storage_url": "https://api.crawlbase.com/storage?rid=rid-1",
                "body": "<html></html>",
            }
        ).encode(),
    }
    fields = extract_fields(response)
    assert fields.screenshot_url.endswith("a.jpg")
    assert fields.rid == "rid-1"
    assert fields.storage_url_present
    assert "screenshot_url" not in response["headers"]


def test_capture_downloads_before_returning(tmp_path):
    seen = {}

    class Client:
        def get(self, url, options):
            seen["options"] = options
            return {
                "status_code": 200,
                "headers": {"cb_status": "200", "url": url},
                "body": json.dumps(
                    {
                        "cb_status": 200,
                        "screenshot_url": "https://shots.example/a.jpg",
                        "rid": "rid-9",
                        "storage_url": "https://api.crawlbase.com/storage?rid=rid-9",
                        "body": "<html><h1>Hi</h1></html>",
                    }
                ).encode(),
            }

    def save(url, dest, timeout):
        assert url.endswith("a.jpg")
        assert timeout == 30
        _jpeg(dest)

    result = capture(
        "https://news.ycombinator.com",
        "desktop",
        token="test-token",
        shots_dir=tmp_path / "shots",
        page_wait=2000,
        scroll_interval=10,
        css_click_selector=".banner",
        timeout=30,
        client=Client(),
        downloader=save,
    )
    assert result.rid == "rid-9"
    assert result.path.is_file()
    assert result.html_path is not None
    assert "Hi" in result.html_path.read_text(encoding="utf-8")
    assert seen["options"]["css_click_selector"] == ".banner,body"
    assert "country" not in seen["options"]


def test_capture_rejects_595_without_downloading(tmp_path):
    class Client:
        def get(self, url, options):
            return {
                "status_code": 200,
                "headers": {},
                "body": json.dumps(
                    {"cb_status": 595, "screenshot_url": "https://shots.example/a.jpg", "rid": "x"}
                ).encode(),
            }

    def save(url, dest, timeout):
        raise AssertionError("download should not run")

    try:
        capture(
            "https://example.com",
            "desktop",
            token="test-token",
            shots_dir=tmp_path,
            client=Client(),
            downloader=save,
            allow_http_fallback=False,
        )
    except CaptureError as exc:
        assert "595" in str(exc)
    else:
        raise AssertionError("expected CaptureError")


def test_redact_strips_credentials():
    raw = "https://api.crawlbase.com/?token=SECRETVALUE&url=1 sk-proj-abcdefghij hooks.slack.com"
    cleaned = redact_secrets("token=SECRETVALUE sk-proj-abcdefghijklmnop https://hooks.slack.com/services/T/B/x")
    assert "SECRETVALUE" not in cleaned
    assert "abcdefghijklmnop" not in cleaned
    assert "services/T" not in cleaned
    assert "REDACTED" in cleaned
    assert "SECRETVALUE" in raw


def test_normalize_records_height_delta_and_crops_to_shared(tmp_path):
    before = _jpeg(tmp_path / "a.jpg", size=(40, 20))
    after = _jpeg(tmp_path / "b.jpg", size=(40, 50))
    pair = normalize_files(before, after, width=40)
    assert pair.before.size == (40, 20)
    assert pair.after.size == (40, 20)
    assert pair.height_delta == 30
    assert pair.before_height == 20
    assert pair.after_height == 50


def test_compare_identical_and_changed(tmp_path):
    same = _jpeg(tmp_path / "same.jpg", size=(80, 80), color=(255, 255, 255))
    pair = normalize_files(same, same, 80)
    quiet = compare_images(pair, tmp_path / "quiet.png", pixel_threshold=24)
    assert quiet.ssim >= 0.99
    assert quiet.changed_area == 0
    assert quiet.heatmap_path.is_file()

    changed = _jpeg(tmp_path / "changed.jpg", size=(80, 80), color=(255, 255, 255), box=(10, 10, 50, 50))
    pair = normalize_files(same, changed, 80)
    loud = compare_images(pair, tmp_path / "loud.png", pixel_threshold=24)
    assert loud.ssim < 0.96
    assert loud.changed_area > 0.03
    assert loud.boxes


def test_gate_boundaries():
    assert crosses(0.97, 0.01, 0.04, 0.03) is False
    assert crosses(0.95, 0.0, 0.04, 0.03) is True
    assert crosses(1.0, 0.03, 0.04, 0.03) is True


def test_parse_explanation_and_jpeg_for_model(tmp_path):
    parsed = parse_explanation("The price reads $29 instead of $19.\nLABEL: pricing")
    assert parsed.label == "pricing"
    assert "29" in parsed.paragraph
    assert "LABEL" not in parsed.paragraph
    inline = parse_explanation("The hero image changed. LABEL: hero")
    assert inline.label == "hero"
    assert inline.paragraph == "The hero image changed."
    assert parse_explanation("LABEL: mystery").label == "other"
    path = _jpeg(tmp_path / "m.jpg", size=(30, 30))
    payload = jpeg_bytes_for_model(path, max_edge=16)
    assert payload.startswith(b"\xff\xd8")


def test_storage_extracts_a_concrete_change_and_skips_other():
    html = "<html><h1>Python</h1><p>Pro $29/mo</p><title>Ignored</title></html>"
    assert text_for_selector(html, "h1") == "Python"
    assert extract_value(html, "", "pricing") == "$29"
    assert needs_html("hero")
    assert not needs_html("other")

    class Client:
        def get(self, rid):
            assert rid == "rid-1"
            return {"status_code": 200, "body": html.encode()}

    assert lookup_change("token", "rid-1", "h1", "hero", client=Client()) == "Python"
    assert lookup_change("token", "rid-1", "h1", "other", client=Client()) == ""


def test_alert_file_carries_score_heatmap_and_rid(tmp_path):
    payload = alert_payload(
        url="https://example.com",
        device="desktop",
        ssim=0.91,
        changed_area=0.08,
        description="The hero image changed.",
        label="hero",
        rid="rid-2",
        heatmap=tmp_path / "heat.png",
        extracted="New headline",
    )
    path = write_alert(tmp_path / "alerts", payload)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["ssim"] == 0.91
    assert saved["rid"] == "rid-2"
    assert saved["heatmap"].endswith("heat.png")
    assert saved["description"].startswith("The hero")


def _shot(url, device, path, rid="rid"):
    return CaptureResult(
        url=url,
        device=device,
        path=path,
        rid=rid,
        cb_status="200",
        envelope_keys=("rid", "screenshot_url"),
        storage_url_present=True,
    )


def test_monitor_baseline_then_quiet_then_alert_and_rotation(tmp_path):
    settings = _settings(tmp_path, rotate_after_n=2)
    shots = []

    def fake_capture(url, device, *, token, shots_dir, **kwargs):
        assert token == "test-token"
        assert kwargs["page_wait"] == 2000
        assert "country" not in kwargs
        path = shots_dir / f"{len(shots)}.jpg"
        if len(shots) < 2:
            _jpeg(path, size=(80, 80), color=(255, 255, 255))
        else:
            _jpeg(path, size=(80, 80), color=(255, 255, 255), box=(5, 5, 60, 60), box_color=(200, 0, 0))
        shots.append(path)
        html_path = path.with_suffix(".html")
        html_path.write_text("<html><h1>Headline</h1></html>", encoding="utf-8")
        return CaptureResult(
            url=url,
            device=device,
            path=path,
            rid=f"rid-{len(shots)}",
            cb_status="200",
            envelope_keys=("body", "screenshot_url"),
            storage_url_present=True,
            html_path=html_path,
        )

    explanations = []

    def fake_explain(before, after, *, api_key, model):
        explanations.append((api_key, model))
        return Explanation("The hero block is red.", "hero")

    stored = []

    def fake_storage(token, rid, selector, label, timeout=120, html_path=None):
        stored.append((rid, label, Path(html_path).read_text(encoding="utf-8") if html_path else ""))
        return "Headline"

    slack_calls = []

    first = run_once(
        settings,
        token="test-token",
        openai_api_key="sk-test",
        capture_fn=fake_capture,
        explain_fn=fake_explain,
        storage_fn=fake_storage,
        slack_fn=lambda url, payload: slack_calls.append(url) or 200,
        slack_webhook="https://hooks.slack.com/services/x",
    )
    assert first[0].phase == "baseline"
    assert first[0].vision is False
    assert slack_calls == []

    second = run_once(
        settings,
        token="test-token",
        openai_api_key="sk-test",
        capture_fn=fake_capture,
        explain_fn=fake_explain,
        storage_fn=fake_storage,
    )
    assert second[0].phase == "compared"
    assert second[0].ssim >= 0.99
    assert second[0].vision is False
    assert second[0].alert_path == ""
    assert explanations == []

    third = run_once(
        settings,
        token="test-token",
        openai_api_key="sk-test",
        openai_model="gpt-4o",
        max_vision_calls=1,
        slack_webhook="https://hooks.slack.com/services/x",
        capture_fn=fake_capture,
        explain_fn=fake_explain,
        storage_fn=fake_storage,
        slack_fn=lambda url, payload: slack_calls.append(payload["rid"]) or 204,
    )
    assert third[0].vision is True
    assert third[0].label == "hero"
    assert third[0].storage is True
    assert third[0].alert_path
    assert stored == [("rid-3", "hero", "<html><h1>Headline</h1></html>")]
    assert slack_calls == ["rid-3"]
    assert third[0].rotated is True
    conn = connect(settings.db_path)
    baseline = get_baseline(conn, "https://news.ycombinator.com", "desktop")
    assert Path(baseline["image_path"]) == shots[-1]
    conn.close()


def test_acknowledge_rotates_the_baseline(tmp_path):
    settings = _settings(tmp_path, rotate_after_n=9)
    images = []

    def fake_capture(url, device, *, token, shots_dir, **kwargs):
        path = shots_dir / f"{len(images)}.jpg"
        _jpeg(path, size=(80, 80), color=(255, 255, 255), box=(0, 0, 40, 40) if images else None)
        images.append(path)
        return _shot(url, device, path, rid=f"r{len(images)}")

    run_once(settings, token="t", capture_fn=fake_capture, openai_api_key=None)
    second = run_once(settings, token="t", capture_fn=fake_capture, openai_api_key=None)
    assert second[0].alert_path
    conn = connect(settings.db_path)
    alert_id = conn.execute("SELECT id FROM alerts").fetchone()["id"]
    acknowledge(conn, alert_id)
    baseline = get_baseline(conn, "https://news.ycombinator.com", "desktop")
    assert baseline["image_path"] == str(images[-1])
    assert conn.execute("SELECT acknowledged FROM alerts WHERE id = ?", (alert_id,)).fetchone()["acknowledged"] == 1
    conn.close()
