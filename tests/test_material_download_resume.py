import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from model.MaterialDuplicateIndex import MaterialDuplicateIndex
from model.MaterialSourceDownloader import (
    _CheckedRangeHttp, _RangeNotHonored, _download_api_file, download_google_drive_source,
)


class FakeRequest:
    def __init__(self):
        self.http = object()


class FakeFiles:
    def get_media(self, **_kwargs):
        return FakeRequest()


class FakeService:
    def files(self):
        return FakeFiles()


class DownloadResumeTests(unittest.TestCase):
    def test_folder_retry_reuses_completed_files_and_resumes_current_file(self):
        payloads = {"video-1": b"complete-one", "video-2": b"part-two"}
        media_calls = []

        class Reply:
            def __init__(self, value):
                self.value = value

            def execute(self):
                return self.value

        class Files:
            def get(self, **_kwargs):
                return Reply({"id": "folder-1", "name": "素材包",
                              "mimeType": "application/vnd.google-apps.folder"})

            def list(self, **_kwargs):
                return Reply({"files": [
                    {"id": key, "name": key + ".mp4", "mimeType": "video/mp4",
                     "size": str(len(value)), "md5Checksum": hashlib.md5(value).hexdigest()}
                    for key, value in payloads.items()
                ]})

            def get_media(self, fileId, **_kwargs):
                media_calls.append(fileId)
                request = FakeRequest()
                request.file_id = fileId
                return request

        class Service:
            def files(self):
                return Files()

        class FakeDownloader:
            interrupted = False
            offsets = []

            def __init__(self, stream, request, chunksize):
                self.stream = stream
                self.request = request
                self._progress = 0

            def next_chunk(self, num_retries=0):
                key = self.request.file_id
                FakeDownloader.offsets.append((key, self._progress))
                if key == "video-2" and not FakeDownloader.interrupted:
                    FakeDownloader.interrupted = True
                    self.stream.write(b"part-")
                    raise OSError("network interrupted")
                self.stream.write(payloads[key][self._progress:])
                return None, True

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            url = "https://drive.google.com/drive/folders/folder-1"
            with patch("googleapiclient.http.MediaIoBaseDownload", FakeDownloader):
                with self.assertRaisesRegex(Exception, "network interrupted"):
                    download_google_drive_source(url, root, service_factory=Service,
                                                 reuse_existing=True)
                first = root / "素材包" / "video-1.mp4"
                self.assertEqual(first.read_bytes(), payloads["video-1"])
                result = download_google_drive_source(
                    url, root, service_factory=Service, reuse_existing=True,
                    duplicate_index=MaterialDuplicateIndex(root),
                )
            self.assertEqual(media_calls, ["video-1", "video-2", "video-2"])
            self.assertEqual(FakeDownloader.offsets[-1], ("video-2", 5))
            self.assertEqual((root / "素材包" / "video-2.mp4").read_bytes(),
                             payloads["video-2"])
            self.assertEqual(len(result.root_paths), 1)
            self.assertEqual(result.root_paths[0], root / "素材包")

    def test_partial_api_file_is_reused_on_second_attempt(self):
        payload = b"first-second"
        metadata = {
            "id": "video-1", "name": "video.mp4", "mimeType": "video/mp4",
            "size": str(len(payload)), "md5Checksum": hashlib.md5(payload).hexdigest(),
        }
        offsets = []

        class FakeDownloader:
            calls = 0

            def __init__(self, stream, request, chunksize):
                self.stream = stream
                self._progress = 0

            def next_chunk(self, num_retries=0):
                offsets.append(self._progress)
                FakeDownloader.calls += 1
                if FakeDownloader.calls == 1:
                    self.stream.write(b"first-")
                    raise OSError("connection lost")
                self.stream.write(b"second")
                return None, True

        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            with patch("googleapiclient.http.MediaIoBaseDownload", FakeDownloader):
                with self.assertRaises(OSError):
                    _download_api_file(FakeService(), metadata, folder, None,
                                       reuse_existing=True)
                self.assertEqual((folder / "video.mp4.part").read_bytes(), b"first-")
                finished = _download_api_file(FakeService(), metadata, folder, None,
                                              reuse_existing=True)
            self.assertEqual(finished.read_bytes(), payload)
            self.assertEqual(offsets, [0, 6])
            self.assertFalse((folder / "video.mp4.part.resume.json").exists())

    def test_range_response_must_match_requested_offset(self):
        class Response(dict):
            status = 200

        class Http:
            def request(self, *_args, **_kwargs):
                return Response(), b"whole file"

        with self.assertRaises(_RangeNotHonored):
            _CheckedRangeHttp(Http()).request(
                "https://example.test/file", "GET", headers={"range": "bytes=6-10"})


if __name__ == "__main__":
    unittest.main()
