"""Local references supplement MCP content; the client owns body rendering."""

import base64
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from workbench_mcp.local_media import add_local_image_references


def image_block(color):
    buffer = io.BytesIO()
    Image.new("RGB", (24, 16), color).save(buffer, format="JPEG")
    return {"type": "image", "mimeType": "image/jpeg",
            "data": base64.b64encode(buffer.getvalue()).decode("ascii")}


class LocalMediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "目录 with spaces" / "image-delivery"

    def test_multiple_images_keep_order_bytes_and_metadata(self):
        original = {"content": [{"type": "text", "text": "names and provenance"},
                                image_block("red"), image_block("blue"),
                                image_block("green")],
                    "structuredContent": {"unchanged": True}}
        result = add_local_image_references(original, self.root)
        self.assertEqual(result["structuredContent"], original["structuredContent"])
        self.assertEqual(result["content"][0], original["content"][0])
        for number, position in enumerate((1, 3, 5)):
            reference = json.loads(result["content"][position]["text"])["display"]
            native = result["content"][position + 1]
            expected_bytes = base64.b64decode(native["data"])
            path = Path(reference["path"])
            self.assertEqual(reference["kind"], "local_file")
            self.assertEqual(reference["image_index"], number)
            self.assertEqual(reference["mime_type"], native["mimeType"])
            self.assertEqual(reference["sha256"], hashlib.sha256(expected_bytes).hexdigest())
            self.assertEqual(native, original["content"][number + 1])
            self.assertTrue(path.is_absolute() and path.is_file())
            self.assertEqual(path.parent, self.root.absolute())
            self.assertIn("目录 with spaces", reference["path"])
            self.assertEqual(path.read_bytes(), expected_bytes)

    def test_page_indices_and_unavailable_item_do_not_shift_three_image_references(self):
        def label(index, file_name, **extra):
            return {"type": "text", "text": json.dumps(
                {"index": index, "file_name": file_name, **extra}, ensure_ascii=False)}

        original = {"content": [
            {"type": "text", "text": '{"offset": 4, "count": 4}'},
            label(4, "第一张.RAW"), image_block("red"),
            label(5, "缺少缩略图.RAW", state="unavailable"),
            label(6, "第二张.RAW", copy_name="副本 1"), image_block("blue"),
            label(7, "第三张.RAW"), image_block("green"),
        ]}
        result = add_local_image_references(original, self.root)
        self.assertEqual([block["type"] for block in result["content"]],
                         ["text", "text", "text", "image", "text",
                          "text", "text", "image", "text", "text", "image"])
        self.assertEqual(result["content"][4], original["content"][3])
        for image_index, label_position, source_position in ((0, 1, 1),
                                                             (1, 5, 4),
                                                             (2, 8, 6)):
            self.assertEqual(result["content"][label_position],
                             original["content"][source_position])
            reference = json.loads(result["content"][label_position + 1]["text"])["display"]
            native = result["content"][label_position + 2]
            self.assertEqual(reference["image_index"], image_index)
            self.assertEqual(native, original["content"][source_position + 1])
            self.assertEqual(Path(reference["path"]).read_bytes(),
                             base64.b64decode(native["data"]))

    def test_reference_failure_keeps_successful_native_result(self):
        original = {"content": [image_block("red")], "isError": False}
        with mock.patch("workbench_mcp.local_media._publish", side_effect=OSError("read only")):
            result = add_local_image_references(original, self.root)
        self.assertFalse(result["isError"])
        self.assertEqual(result["content"][-1], original["content"][0])
        self.assertEqual(json.loads(result["content"][0]["text"])["display"]["kind"], "unavailable")

    def test_no_images_does_not_create_a_cache(self):
        original = {"content": [{"type": "text", "text": "config only"}]}
        self.assertEqual(add_local_image_references(original, self.root), original)
        self.assertFalse(self.root.exists())

    def test_corrupt_cached_delivery_is_repaired(self):
        original = {"content": [image_block("red")]}
        first = add_local_image_references(original, self.root)
        path = Path(json.loads(first["content"][0]["text"])["display"]["path"])
        path.write_bytes(b"broken image")
        self.assertEqual(add_local_image_references(original, self.root), first)
        self.assertEqual(path.read_bytes(), base64.b64decode(original["content"][0]["data"]))


if __name__ == "__main__":
    unittest.main()
