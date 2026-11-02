"""聊天附件：图片走多模态、文本内联，类型与大小都要卡住。"""
import base64
import unittest

from ai.agent import (
    MAX_ATTACHMENTS, MAX_IMAGE_BYTES, _attachments, _user_content,
)


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 64


class AttachmentValidationTest(unittest.TestCase):
    def test_image_becomes_multimodal_part(self):
        atts = _attachments([{"kind": "image", "name": "s.png", "mime": "image/png",
                              "data": b64(PNG)}])
        self.assertEqual(len(atts), 1)
        content = _user_content("看看这个", atts)
        self.assertIsInstance(content, list)
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_data_url_prefix_is_accepted(self):
        atts = _attachments([{"kind": "image", "name": "s.png", "mime": "image/png",
                              "data": "data:image/png;base64," + b64(PNG)}])
        self.assertEqual(atts[0]["data"], b64(PNG))

    def test_text_file_is_inlined_not_multimodal(self):
        atts = _attachments([{"kind": "text", "name": "a.log", "mime": "text/plain",
                              "data": b64("错误日志".encode())}])
        content = _user_content("分析", atts)
        self.assertIsInstance(content, str)
        self.assertIn("a.log", content)
        self.assertIn("错误日志", content)

    def test_oversized_image_rejected(self):
        huge = b64(b"x" * (MAX_IMAGE_BYTES + 100))
        with self.assertRaises(ValueError):
            _attachments([{"kind": "image", "name": "big.png", "mime": "image/png", "data": huge}])

    def test_unsupported_types_rejected(self):
        for bad in (
            {"kind": "image", "name": "x.tiff", "mime": "image/tiff", "data": b64(b"aa")},
            {"kind": "video", "name": "v.mp4", "mime": "video/mp4", "data": b64(b"aa")},
            {"kind": "text", "name": "x", "mime": "text/plain", "data": ""},
        ):
            with self.assertRaises(ValueError):
                _attachments([bad])

    def test_attachment_count_is_capped(self):
        items = [{"kind": "text", "name": f"f{i}.txt", "mime": "text/plain",
                  "data": b64(b"a")} for i in range(MAX_ATTACHMENTS + 1)]
        with self.assertRaises(ValueError):
            _attachments(items)

    def test_no_attachments_keeps_plain_string(self):
        self.assertEqual(_user_content("你好", []), "你好")


if __name__ == "__main__":
    unittest.main()
