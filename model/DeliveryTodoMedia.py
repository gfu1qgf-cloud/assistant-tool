"""Small previews and conservative folder-link checks for delivery to-dos."""

from collections import defaultdict
from pathlib import Path

from model.GoogleDriveHelper import GOOGLE_FOLDER_MIME, drive_folder_link


def candidate_folders(items):
    """Only files visible in this one send batch can authorize a folder link."""
    folders = defaultdict(set)
    for item in items:
        folder_id = str(item.get("folder_id") or "").strip()
        file_id = str(item.get("drive_file_id") or "").strip()
        if folder_id and file_id:
            folders[folder_id].add(file_id)
    return dict(folders)


def verify_sendable_folders(items, service):
    """Never expose a folder containing an unknown, unapproved, or nested item."""
    result = {}
    for folder_id, expected in candidate_folders(items).items():
        actual = set()
        page_token = None
        while True:
            response = service.files().list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields="nextPageToken,files(id,mimeType)",
                pageSize=1000,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            for child in response.get("files", []):
                if child.get("mimeType") == GOOGLE_FOLDER_MIME:
                    actual.add("nested-folder:" + str(child.get("id")))
                else:
                    actual.add(str(child.get("id") or ""))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        if actual and actual == expected:
            result[folder_id] = drive_folder_link(folder_id)
    return result


def thumbnail_bytes(item, service=None):
    """Return a tiny JPEG from the local video, then try Drive's thumbnail."""
    local_file = Path(str(item.get("local_file") or ""))
    if local_file.is_file():
        try:
            import cv2

            capture = cv2.VideoCapture(str(local_file))
            try:
                if capture.isOpened():
                    capture.set(cv2.CAP_PROP_POS_MSEC, 500)
                    ok, frame = capture.read()
                    if not ok:
                        capture.set(cv2.CAP_PROP_POS_MSEC, 0)
                        ok, frame = capture.read()
                    if ok and frame is not None:
                        height, width = frame.shape[:2]
                        if not width or not height:
                            return b""
                        scale = min(160 / width, 96 / height, 1)
                        frame = cv2.resize(frame, (max(1, int(width * scale)),
                                                   max(1, int(height * scale))))
                        encoded, data = cv2.imencode(".jpg", frame)
                        if encoded:
                            return data.tobytes()
            finally:
                capture.release()
        except Exception:
            pass
    file_id = str(item.get("drive_file_id") or "").strip()
    if not service or not file_id:
        return b""
    try:
        metadata = service.files().get(
            fileId=file_id, fields="thumbnailLink", supportsAllDrives=True,
        ).execute()
        url = str(metadata.get("thumbnailLink") or "")
        if not url:
            return b""
        response, content = service._http.request(url)
        if int(getattr(response, "status", 0)) == 200 and len(content) <= 2_000_000:
            return bytes(content)
    except Exception:
        pass
    return b""
