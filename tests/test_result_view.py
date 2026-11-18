"""The colored result is an isolated, expiring view of the matching saved answer."""

import hashlib
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

pytest.importorskip("markdown_it")

from session_bridge.service import Service
from session_bridge.state import Store
from session_bridge.viewer import HistoryViewer


def test_chat_shows_body_even_with_viewer_and_explicit_links_are_scoped(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    svc = Service(store, {"client_secret": "private-placeholder-token"})
    store.pair("tenant", "user", "chat")
    alias = store.upsert_session("win", "codex", "id", "", {"title": "测试会话 <script>"})
    viewer = HistoryViewer(svc, port=0)
    svc.viewer = viewer
    viewer.start()
    result = (
        "| 任务 | 状态 |\n| --- | --- |\n| 读取 | 完成 |\n\n"
        "```python\nprint(123)\n```\n\n<svg onload=alert(1)>\n\n"
        "[危险](javascript:alert(1))\n\n![远程图片](https://example.org/probe.png)\n\n"
        "测试 private-placeholder-token"
    )
    try:
        svc.ingest(
            "win",
            {
                "events": [
                    {
                        "engine": "codex",
                        "sid": "id",
                        "event_id": "done",
                        "kind": "completed",
                        "result": result,
                    }
                ]
            },
        )
        body = store.db.execute("SELECT body FROM outbox").fetchone()[0]
        heading, text = body.split("\n\n", 1)
        assert heading == "> 测试会话 &lt;script&gt;"
        assert "| --- | --- |" in text and "```python\nprint(123)\n```" in text
        assert "private-placeholder-token" not in body
        link = viewer.result_link(alias, "win:done")
        with urlopen(link, timeout=3) as res:
            page = res.read().decode()
            assert res.headers["Referrer-Policy"] == "no-referrer"
            assert res.headers["Cache-Control"] == "no-store"
            assert "default-src 'none'" in res.headers["Content-Security-Policy"]
        assert 'class="status success"' in page and 'class="session"' in page
        assert '<div class="table-scroll"' in page and "<table>" in page
        assert "<pre><code" in page and "测试会话 &lt;script&gt;" in page
        assert "private-placeholder-token" not in page and "<svg" not in page
        assert 'href="javascript:' not in page and "<img" not in page
        document = viewer.document_link("用户原话", "> 原话一\n\n> 原话二")
        with urlopen(document, timeout=3) as res:
            transcript = res.read().decode()
        assert "原话一" in transcript and "原话二" in transcript
        with pytest.raises(HTTPError) as exc:
            urlopen(document.replace("/result/", "/session/"), timeout=3)
        assert exc.value.code == 403
        with pytest.raises(HTTPError) as exc:
            urlopen(link.replace("/result/", "/session/"), timeout=3)
        assert exc.value.code == 403

        svc.ingest(
            "win",
            {
                "events": [
                    {
                        "engine": "codex",
                        "sid": "id",
                        "event_id": "failed",
                        "kind": "failed",
                        "result": "HTTP 401\n\n原始错误不可见性回归",
                    }
                ]
            },
        )
        body = store.db.execute("SELECT body FROM outbox ORDER BY id DESC LIMIT 1").fetchone()[0]
        assert "HTTP 401" in body and "原始错误不可见性回归" in body
        history = viewer.link(alias)
        with pytest.raises(HTTPError) as exc:
            urlopen(history.replace("/session/", "/result/"), timeout=3)
        assert exc.value.code == 403
        key = "view:" + hashlib.sha256(link.rsplit("/", 1)[1].encode()).hexdigest()
        store.put(key, dict(store.get(key), expires=1))
        with pytest.raises(HTTPError) as exc:
            urlopen(link, timeout=3)
        assert exc.value.code == 403
    finally:
        viewer.server.shutdown()
        viewer.server.server_close()
        store.close()
