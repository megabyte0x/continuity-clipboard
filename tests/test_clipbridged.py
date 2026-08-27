import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PLUGIN_DIR)

import clipbridged


class SniffMimeTest(unittest.TestCase):
    def test_heic_ftyp_brands(self):
        self.assertEqual(clipbridged.sniff_mime(b"\x00\x00\x00\x1cftypheic" + b"\x00" * 8), "image/heic")
        self.assertEqual(clipbridged.sniff_mime(b"\x00\x00\x00\x18ftypmif1" + b"\x00" * 8), "image/heic")

    def test_avif_brand(self):
        self.assertEqual(clipbridged.sniff_mime(b"\x00\x00\x00\x1cftypavif" + b"\x00" * 8), "image/avif")

    def test_tiff_magics(self):
        self.assertEqual(clipbridged.sniff_mime(b"II*\x00restoftiff"), "image/tiff")
        self.assertEqual(clipbridged.sniff_mime(b"MM\x00*restoftiff"), "image/tiff")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class BridgeServerTest(unittest.TestCase):
    def setUp(self):
        self.fake_dir = tempfile.mkdtemp(prefix="clipbridge-fake-")
        self.state_dir = tempfile.mkdtemp(prefix="clipbridge-state-")
        os.environ["FAKE_DIR"] = self.fake_dir
        self._old_path = os.environ["PATH"]
        os.environ["PATH"] = os.path.join(PLUGIN_DIR, "tests", "bin") + os.pathsep + self._old_path
        self._old_max = clipbridged.MAX_BODY_BYTES
        self.server = clipbridged.ThreadingHTTPServer(("127.0.0.1", 0), clipbridged.Handler)
        self.server.daemon_threads = True
        self.server.bridge = clipbridged.Bridge(self.state_dir, self.server.server_address[1])
        self.server.bridge.write_status()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        os.environ["PATH"] = self._old_path
        clipbridged.MAX_BODY_BYTES = self._old_max
        shutil.rmtree(self.fake_dir, ignore_errors=True)
        shutil.rmtree(self.state_dir, ignore_errors=True)

    def request(self, path, data=None, headers=None, method=None):
        req = urllib.request.Request(self.base + path, data=data, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def set_clip(self, types, content=b""):
        with open(os.path.join(self.fake_dir, "types"), "w") as handle:
            handle.write("".join(t + "\n" for t in types))
        with open(os.path.join(self.fake_dir, "content"), "wb") as handle:
            handle.write(content)

    def auth(self):
        return {"X-Clip-Token": self.server.bridge.token}

    # -- auth ----------------------------------------------------------

    def test_ping_needs_no_token(self):
        status, _, body = self.request("/ping")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_missing_token_rejected(self):
        status, _, _ = self.request("/clip")
        self.assertEqual(status, 401)

    def test_bad_token_rejected(self):
        status, _, _ = self.request("/clip", headers={"X-Clip-Token": "wrong"})
        self.assertEqual(status, 401)

    def test_query_token_accepted(self):
        self.set_clip(["text/plain"], b"hello")
        status, _, _ = self.request(f"/clip?token={self.server.bridge.token}")
        self.assertEqual(status, 200)

    # -- desktop -> phone ----------------------------------------------

    def test_get_text_clip(self):
        self.set_clip(["text/plain;charset=utf-8", "UTF8_STRING"], "héllo".encode())
        status, headers, body = self.request("/clip", headers=self.auth())
        self.assertEqual(status, 200)
        self.assertIn("text/plain", headers["Content-Type"])
        self.assertEqual(body.decode(), "héllo")

    def test_get_empty_clip_is_204(self):
        self.set_clip([])
        status, _, body = self.request("/clip", headers=self.auth())
        self.assertEqual(status, 204)
        self.assertEqual(body, b"")

    def test_get_image_clip(self):
        png = b"\x89PNG\r\n\x1a\nfakepixels"
        self.set_clip(["image/png", "text/html"], png)
        status, headers, body = self.request("/clip", headers=self.auth())
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(body, png)

    # -- phone -> desktop ----------------------------------------------

    def test_post_text_sets_clipboard(self):
        status, _, body = self.request(
            "/clip", data="hi from phone".encode(), method="POST",
            headers={**self.auth(), "Content-Type": "text/plain"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        with open(os.path.join(self.fake_dir, "wl-copy.data"), "rb") as handle:
            self.assertEqual(handle.read(), b"hi from phone")
        with open(os.path.join(self.fake_dir, "notify-send.log")) as handle:
            self.assertIn("hi from phone", handle.read())

    def test_post_image_sets_typed_clipboard(self):
        png = b"\x89PNG\r\n\x1a\nfakepixels"
        status, _, _ = self.request(
            "/clip", data=png, method="POST",
            headers={**self.auth(), "Content-Type": "image/png"})
        self.assertEqual(status, 200)
        with open(os.path.join(self.fake_dir, "wl-copy.args")) as handle:
            self.assertIn("--type image/png", handle.read())
        with open(os.path.join(self.fake_dir, "wl-copy.data"), "rb") as handle:
            self.assertEqual(handle.read(), png)

    def test_post_jpeg_is_converted_to_png(self):
        jpeg = b"\xff\xd8\xff\xe0fakejpegpixels"
        status, _, _ = self.request(
            "/clip", data=jpeg, method="POST",
            headers={**self.auth(), "Content-Type": "image/jpeg"})
        self.assertEqual(status, 200)
        with open(os.path.join(self.fake_dir, "wl-copy.args")) as handle:
            self.assertIn("--type image/png", handle.read())
        with open(os.path.join(self.fake_dir, "wl-copy.data"), "rb") as handle:
            self.assertEqual(handle.read(), b"\x89PNG\r\n\x1a\nCONVERTED")

    def test_post_form_encoded_text(self):
        status, headers, _ = self.request(
            "/clip", data=b"text=from+safari%21", method="POST",
            headers={**self.auth(), "Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        with open(os.path.join(self.fake_dir, "wl-copy.data"), "rb") as handle:
            self.assertEqual(handle.read(), b"from safari!")

    def test_get_with_text_param_sends_to_clipboard(self):
        import urllib.parse
        text = "https://x.com/user/status/123?s=46"
        status, _, body = self.request(
            f"/clip?token={self.server.bridge.token}&text={urllib.parse.quote(text)}")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        with open(os.path.join(self.fake_dir, "wl-copy.data"), "rb") as handle:
            self.assertEqual(handle.read(), text.encode())

    def test_get_with_empty_text_param_is_400(self):
        status, _, _ = self.request(f"/clip?token={self.server.bridge.token}&text=")
        self.assertEqual(status, 400)

    def test_post_whitespace_only_text_is_422_with_guidance(self):
        status, _, body = self.request(
            "/clip", data=b"\n  \n", method="POST",
            headers={**self.auth(), "Content-Type": "text/plain"})
        self.assertEqual(status, 422)
        self.assertIn("Clipboard variable", json.loads(body)["error"])
        with open(os.path.join(self.fake_dir, "notify-send.log")) as handle:
            self.assertIn("empty clip", handle.read())

    def test_post_with_real_text_query_wins_over_body(self):
        import urllib.parse
        url = "https://x.com/theo/status/2092838987304174031?s=46"
        status, _, _ = self.request(
            f"/clip?token={self.server.bridge.token}&text={urllib.parse.quote(url)}",
            data=b"ignored body", method="POST",
            headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 200)
        with open(os.path.join(self.fake_dir, "wl-copy.data"), "rb") as handle:
            self.assertEqual(handle.read(), url.encode())

    def test_post_literal_text_query_defers_to_image_body(self):
        png = b"\x89PNG\r\n\x1a\nrealpixels"
        status, _, _ = self.request(
            f"/clip?token={self.server.bridge.token}&text=Clipboard",
            data=png, method="POST",
            headers={"Content-Type": "image/png"})
        self.assertEqual(status, 200)
        with open(os.path.join(self.fake_dir, "wl-copy.args")) as handle:
            self.assertIn("--type image/png", handle.read())
        with open(os.path.join(self.fake_dir, "wl-copy.data"), "rb") as handle:
            self.assertEqual(handle.read(), png)

    def test_post_literal_text_query_no_body_gets_guidance(self):
        status, _, body = self.request(
            f"/clip?token={self.server.bridge.token}&text=Clipboard",
            method="POST", headers={"Content-Length": "0"})
        self.assertEqual(status, 200)
        self.assertIn("hint", json.loads(body))
        self.assertFalse(os.path.exists(os.path.join(self.fake_dir, "wl-copy.data")))
        with open(os.path.join(self.fake_dir, "notify-send.log")) as handle:
            self.assertIn("blue Clipboard variable", handle.read())

    def test_literal_clipboard_word_gets_guidance(self):
        status, _, body = self.request(
            "/clip", data=b"Clipboard", method="POST",
            headers={**self.auth(), "Content-Type": "text/plain"})
        self.assertEqual(status, 200)
        self.assertIn("hint", json.loads(body))
        with open(os.path.join(self.fake_dir, "wl-copy.data"), "rb") as handle:
            self.assertEqual(handle.read(), b"Clipboard")
        with open(os.path.join(self.fake_dir, "notify-send.log")) as handle:
            self.assertIn("blue variable token", handle.read())

    def test_literal_clipboard_word_in_text_param_gets_guidance(self):
        status, _, body = self.request(f"/clip?token={self.server.bridge.token}&text=Clipboard")
        self.assertEqual(status, 200)
        self.assertIn("hint", json.loads(body))
        with open(os.path.join(self.fake_dir, "notify-send.log")) as handle:
            self.assertIn("blue variable token", handle.read())

    def test_put_and_patch_work_like_post(self):
        for method in ("PUT", "PATCH"):
            status, _, _ = self.request(
                "/clip", data=f"via {method}".encode(), method=method,
                headers={**self.auth(), "Content-Type": "text/plain"})
            self.assertEqual(status, 200)
            with open(os.path.join(self.fake_dir, "wl-copy.data"), "rb") as handle:
                self.assertEqual(handle.read(), f"via {method}".encode())

    def test_get_with_body_is_treated_as_send(self):
        status, _, _ = self.request(
            "/clip", data=b"forgot the method picker", method="GET",
            headers={**self.auth(), "Content-Type": "text/plain"})
        self.assertEqual(status, 200)
        with open(os.path.join(self.fake_dir, "wl-copy.data"), "rb") as handle:
            self.assertEqual(handle.read(), b"forgot the method picker")

    def test_post_octet_stream_sniffs_text(self):
        status, _, _ = self.request(
            "/clip", data=b"plain words", method="POST",
            headers={**self.auth(), "Content-Type": "application/octet-stream"})
        self.assertEqual(status, 200)
        with open(os.path.join(self.fake_dir, "wl-copy.data"), "rb") as handle:
            self.assertEqual(handle.read(), b"plain words")

    def test_post_octet_stream_sniffs_png(self):
        png = b"\x89PNG\r\n\x1a\nfakepixels"
        status, _, _ = self.request(
            "/clip", data=png, method="POST",
            headers={**self.auth(), "Content-Type": "application/octet-stream"})
        self.assertEqual(status, 200)
        with open(os.path.join(self.fake_dir, "wl-copy.args")) as handle:
            self.assertIn("--type image/png", handle.read())

    def test_post_heic_is_converted_to_png(self):
        heic = b"\x00\x00\x00\x1cftypheic" + b"\x00" * 24
        status, _, _ = self.request(
            "/clip", data=heic, method="POST",
            headers={**self.auth(), "Content-Type": "image/heic"})
        self.assertEqual(status, 200)
        with open(os.path.join(self.fake_dir, "wl-copy.args")) as handle:
            self.assertIn("--type image/png", handle.read())
        with open(os.path.join(self.fake_dir, "wl-copy.data"), "rb") as handle:
            self.assertEqual(handle.read(), b"\x89PNG\r\n\x1a\nCONVERTED")

    def test_post_octet_stream_heic_sniffed_and_converted(self):
        heic = b"\x00\x00\x00\x1cftypheic" + b"\x00" * 24
        status, _, _ = self.request(
            "/clip", data=heic, method="POST",
            headers={**self.auth(), "Content-Type": "application/octet-stream"})
        self.assertEqual(status, 200)
        with open(os.path.join(self.fake_dir, "wl-copy.args")) as handle:
            self.assertIn("--type image/png", handle.read())

    def test_post_png_is_not_converted(self):
        png = b"\x89PNG\r\n\x1a\noriginalpixels"
        status, _, _ = self.request(
            "/clip", data=png, method="POST",
            headers={**self.auth(), "Content-Type": "image/png"})
        self.assertEqual(status, 200)
        with open(os.path.join(self.fake_dir, "wl-copy.data"), "rb") as handle:
            self.assertEqual(handle.read(), png)

    def test_post_too_large_is_413(self):
        clipbridged.MAX_BODY_BYTES = 16
        status, _, _ = self.request(
            "/clip", data=b"x" * 64, method="POST",
            headers={**self.auth(), "Content-Type": "text/plain"})
        self.assertEqual(status, 413)

    def test_post_unsupported_type_is_415(self):
        status, _, _ = self.request(
            "/clip", data=b"\x00\x01\x02", method="POST",
            headers={**self.auth(), "Content-Type": "application/zip"})
        self.assertEqual(status, 415)

    # -- state files ----------------------------------------------------

    def test_token_file_created_private(self):
        path = os.path.join(self.state_dir, "token")
        self.assertTrue(os.path.exists(path))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertGreater(len(self.server.bridge.token), 20)

    def test_status_json_counts_serves(self):
        self.set_clip(["text/plain"], b"payload")
        self.request("/clip", headers=self.auth())
        with open(os.path.join(self.state_dir, "status.json")) as handle:
            status = json.load(handle)
        self.assertEqual(status["served"], 1)
        self.assertEqual(status["port"], self.server.server_address[1])
        self.assertEqual(status["last_event"]["direction"], "desktop-to-phone")
        self.assertIn("payload", status["last_event"]["preview"])

    # -- pages -----------------------------------------------------------

    def test_setup_page_contains_pairing_urls(self):
        status, headers, body = self.request("/setup", headers=self.auth())
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        page = body.decode()
        self.assertIn("/clip", page)
        self.assertIn(self.server.bridge.token, page)
        self.assertIn("X-Clip-Token", page)
        # The zero-tap automation recipes are part of the product promise.
        self.assertIn("Back Tap", page)
        self.assertIn("NFC", page)
        self.assertIn("Is Opened", page)

    def test_setup_page_without_links_offers_editor_and_publish_hint(self):
        _, _, body = self.request("/setup", headers=self.auth())
        page = body.decode()
        self.assertIn("shortcuts://create-shortcut", page)
        self.assertIn("shortcut-links.json", page)
        self.assertNotIn("One-tap install", page)

    def test_setup_page_with_links_shows_one_tap_install(self):
        links = {
            "send": "https://www.icloud.com/shortcuts/aaaa1111",
            "get": "https://www.icloud.com/shortcuts/bbbb2222",
        }
        with open(os.path.join(self.state_dir, "shortcut-links.json"), "w") as handle:
            json.dump(links, handle)
        _, _, body = self.request("/setup", headers=self.auth())
        page = body.decode()
        self.assertIn("One-tap install", page)
        self.assertIn(links["send"], page)
        self.assertIn(links["get"], page)

    def test_setup_page_rejects_non_icloud_links(self):
        links = {"send": "https://evil.example/x", "get": "https://www.icloud.com/shortcuts/ok"}
        with open(os.path.join(self.state_dir, "shortcut-links.json"), "w") as handle:
            json.dump(links, handle)
        _, _, body = self.request("/setup", headers=self.auth())
        page = body.decode()
        self.assertNotIn("evil.example", page)
        self.assertNotIn("One-tap install", page)

    def test_root_redirects_to_setup(self):
        opener = urllib.request.build_opener(NoRedirect)
        req = urllib.request.Request(self.base + "/", headers=self.auth())
        try:
            response = opener.open(req, timeout=5)
            status, location = response.status, response.headers.get("Location", "")
        except urllib.error.HTTPError as error:
            status, location = error.code, error.headers.get("Location", "")
        self.assertEqual(status, 302)
        self.assertIn("/setup", location)

    def test_unknown_route_404(self):
        status, _, _ = self.request("/nope", headers=self.auth())
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
