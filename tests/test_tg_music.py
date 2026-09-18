import os
import tempfile
import types
import unittest

import p2
import tg_music


class TgMusicHelperTests(unittest.TestCase):
    def test_sanitize_filename(self):
        self.assertEqual(tg_music.sanitize_filename('a/b:c*?"<>|d'), "a_b_c_d")
        self.assertEqual(tg_music.sanitize_filename("  ..  "), "audio")
        self.assertEqual(tg_music.sanitize_filename(""), "audio")

    def test_write_m3u(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = os.path.join(tmp, "song.mp3")
            open(audio, "w").close()
            m3u = os.path.join(tmp, "telegram.m3u")
            tg_music.write_m3u(m3u, [("Artist - Title", audio)])
            with open(m3u, encoding="utf-8") as f:
                text = f.read()
        self.assertIn("#EXTM3U", text)
        self.assertIn("#EXTINF:-1,Artist - Title", text)
        self.assertIn(audio, text)

    def test_audio_ext(self):
        msg = types.SimpleNamespace(file=types.SimpleNamespace(ext=".flac"))
        self.assertEqual(tg_music.audio_ext(msg), ".flac")
        msg_unknown = types.SimpleNamespace(file=types.SimpleNamespace(ext=None))
        self.assertEqual(tg_music.audio_ext(msg_unknown), ".mp3")
        self.assertEqual(tg_music.audio_ext(types.SimpleNamespace(file=None)), ".mp3")

    def test_local_path_from_url(self):
        self.assertEqual(
            p2.MediaPlayer._local_path_from_url("file:///tmp/a.mp3"), "/tmp/a.mp3"
        )
        self.assertIsNone(p2.MediaPlayer._local_path_from_url("http://x/a.mp3"))
        self.assertIsNone(p2.MediaPlayer._local_path_from_url("rtsp://x/a"))
        self.assertEqual(
            p2.MediaPlayer._local_path_from_url("/tmp/a.mp3"), "/tmp/a.mp3"
        )
        self.assertIsNone(p2.MediaPlayer._local_path_from_url(""))

    def test_is_audio_document_and_message(self):
        doc_audio = types.SimpleNamespace(
            document=types.SimpleNamespace(mime_type="audio/mpeg"),
            file=types.SimpleNamespace(ext=".mp3"),
            audio=None,
        )
        self.assertTrue(tg_music.is_audio_document(doc_audio))
        self.assertTrue(tg_music.is_audio_message(doc_audio))

        # 以普通文件形式发送、靠扩展名识别
        doc_by_ext = types.SimpleNamespace(
            document=types.SimpleNamespace(mime_type="application/octet-stream"),
            file=types.SimpleNamespace(ext=".flac"),
            audio=None,
        )
        self.assertTrue(tg_music.is_audio_document(doc_by_ext))

        doc_video = types.SimpleNamespace(
            document=types.SimpleNamespace(mime_type="video/mp4"),
            file=types.SimpleNamespace(ext=".mp4"),
            audio=None,
        )
        self.assertFalse(tg_music.is_audio_document(doc_video))
        self.assertFalse(tg_music.is_audio_message(doc_video))

        music = types.SimpleNamespace(
            document=None, file=types.SimpleNamespace(ext=".mp3"),
            audio=types.SimpleNamespace(title="T", performer="P"),
        )
        self.assertTrue(tg_music.is_audio_message(music))

    def test_message_audio_title(self):
        music = types.SimpleNamespace(
            id=1, document=None,
            file=types.SimpleNamespace(name="x.mp3", ext=".mp3"),
            audio=types.SimpleNamespace(title="Title", performer="Artist"),
        )
        self.assertEqual(tg_music.message_audio_title(music), "Artist - Title")

        only_title = types.SimpleNamespace(
            id=2, document=None,
            file=types.SimpleNamespace(name="x.mp3", ext=".mp3"),
            audio=types.SimpleNamespace(title="OnlyTitle", performer=None),
        )
        self.assertEqual(tg_music.message_audio_title(only_title), "OnlyTitle")

        doc = types.SimpleNamespace(
            id=3, document=types.SimpleNamespace(mime_type="audio/mpeg"),
            file=types.SimpleNamespace(name="song name.flac", ext=".flac"),
            audio=None,
        )
        self.assertEqual(tg_music.message_audio_title(doc), "song name")

        unnamed = types.SimpleNamespace(
            id=7, document=types.SimpleNamespace(mime_type="audio/mpeg"),
            file=None, audio=None,
        )
        self.assertEqual(tg_music.message_audio_title(unnamed), "audio_7")

    def test_audio_ext_mime_fallback(self):
        msg = types.SimpleNamespace(
            file=types.SimpleNamespace(ext=None),
            document=types.SimpleNamespace(mime_type="audio/flac"),
        )
        self.assertEqual(tg_music.audio_ext(msg), ".flac")
        msg2 = types.SimpleNamespace(
            file=None,
            document=types.SimpleNamespace(mime_type="audio/ogg"),
        )
        self.assertEqual(tg_music.audio_ext(msg2), ".ogg")

    def test_env_credentials(self):
        saved_env = dict(os.environ)
        saved_cfg = tg_music.CONFIG_FILE
        try:
            os.environ["TG_API_ID"] = " 12345 "
            os.environ["TG_API_HASH"] = "abc"
            os.environ.pop("TG_PHONE", None)
            self.assertEqual(
                tg_music.get_env_credentials(), ("12345", "abc", "")
            )
            self.assertEqual(tg_music.env_value("TG_PHONE"), "")
        finally:
            tg_music.CONFIG_FILE = saved_cfg
            os.environ.clear()
            os.environ.update(saved_env)

    def test_update_config_strips_credentials_when_env_set(self):
        saved_env = dict(os.environ)
        saved_cfg = tg_music.CONFIG_FILE
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tg_music.CONFIG_FILE = os.path.join(tmp, "cfg.json")
                os.environ["TG_API_ID"] = "1"
                os.environ["TG_API_HASH"] = "h"
                tg_music.update_config(
                    api_id="1", api_hash="h", phone="p", channels="@c"
                )
                data = tg_music.load_config()
            self.assertNotIn("api_id", data)
            self.assertNotIn("api_hash", data)
            self.assertNotIn("phone", data)
            self.assertEqual(data.get("channels"), "@c")
        finally:
            tg_music.CONFIG_FILE = saved_cfg
            os.environ.clear()
            os.environ.update(saved_env)


if __name__ == "__main__":
    unittest.main()
