"""Smoke test that the downloadable archive is self-contained and safe to open."""
import json
import io
import re
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import app as viewer


class ExportHtmlTest(unittest.TestCase):
    def test_text_quote_recall_and_image_are_embedded(self):
        with tempfile.TemporaryDirectory() as directory:
            picture = Path(directory) / "photo.png"
            picture.write_bytes(b"\x89PNG\r\n\x1a\nimage")

            def fake_query(sql, params=()):
                if "FROM messages m LEFT JOIN" in sql:
                    return [{"msgid": "one", "action": "send", "msgtime": 1762243200000,
                             "msgtype": "text", "content": "", "raw_json": json.dumps({"text": {"content": "这是一条引用/回复消息：\n张三：\n旧图片\n-----\n回复内容 </script>"}}),
                             "from_user": "A1", "sender_name": "李四", "is_employee": True}]
                if "FROM media_files WHERE msgid" in sql:
                    return [{"id": 1, "msgid": "one", "msgtype": "image", "filepath": str(picture),
                             "local_path": None, "filename": "photo.png", "file_name": "photo.png", "oss_url": None}]
                if "msgtype='revoke'" in sql:
                    return [{"pre_msgid": "one"}]
                if "FROM wecom_rooms WHERE roomid" in sql:
                    return [{"room_name": "测试群"}]
                raise AssertionError(sql)

            with patch.object(viewer, "query", side_effect=fake_query), patch.object(viewer, "local_media_path", return_value=picture):
                with viewer.app.test_request_context("/api/export?conversation=room:test"):
                    response = viewer.export_conversation.__wrapped__()
            html = response.get_data(as_text=True)
            self.assertIn("attachment; filename=conversation.html", response.headers["Content-Disposition"])
            data = json.loads(re.search(r'<script id="archive-data" type="application/json">(.*?)</script>', html, re.S).group(1))
            message = data["messages"][0]
            self.assertEqual(message["quote"], {"sender": "张三", "content": "旧图片"})
            self.assertTrue(message["recalled"])
            self.assertIn("回复内容 </script>", message["content"])
            self.assertTrue(message["attachments"][0]["data"].startswith("data:image/png;base64,"))
            self.assertNotIn("回复内容 </script>", html)

    def test_batch_download_checks_group_and_reports_missing_media(self):
        with tempfile.TemporaryDirectory() as directory:
            document = Path(directory) / "report.txt"
            document.write_text("文件内容", encoding="utf-8")
            rows = [{"id": 1, "filepath": str(document), "local_path": None,
                     "filename": "report.txt", "file_name": "report.txt"},
                    {"id": 2, "filepath": None, "local_path": None,
                     "filename": "missing.pdf", "file_name": "missing.pdf"}]
            with patch.object(viewer, "query", return_value=rows), patch.object(viewer, "local_media_path", side_effect=[document, None]):
                with viewer.app.test_request_context("/api/files/batch-download", method="POST", json={"roomid": "room-1", "ids": [1, 2]}):
                    response = viewer.batch_download_files.__wrapped__()
            response.direct_passthrough = False
            with zipfile.ZipFile(io.BytesIO(response.get_data())) as archive:
                self.assertEqual(archive.read("1-report.txt").decode("utf-8"), "文件内容")
                self.assertIn("missing.pdf", archive.read("未能打包的文件.txt").decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
