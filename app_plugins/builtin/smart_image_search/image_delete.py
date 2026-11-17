"""Delete selected library images using the explicitly confirmed deletion mode."""
import logging
import uuid
from model.AppDataStorage import application_storage, data_file, data_directory, data_io
from model.InventoryManager import MATERIAL_IMAGE_SUFFIXES
from model.image_retrieval.index import DELETED_IMAGES_FOLDER, is_deleted_image_path


def trash_selected(paths, roots, progress, cancelled, *, permanent=False):
    storage = application_storage()
    roots = [storage.check(root).resolve() for root in roots]
    deleted, moved, failures, failure_reasons = [], [], [], []
    batch = "批次-" + uuid.uuid4().hex[:12]
    for number, raw in enumerate(paths, 1):
        if cancelled():
            break
        try:
            # Check the live data volume before touching any file. Reject links
            # and directories; never recurse or follow a link outside a library.
            path = storage.check(raw)
            if path.is_symlink() or not path.is_file():
                raise OSError("原图不存在、不是文件或是符号链接")
            resolved = path.resolve()
            matches = [root for root in roots if root in resolved.parents]
            if not matches or is_deleted_image_path(resolved):
                raise OSError("图片不在当前选定的图片库内")
            if path.suffix.lower() not in MATERIAL_IMAGE_SUFFIXES:
                raise OSError("不是支持的图片文件")
            if permanent:
                data_file(path).remove()
            else:
                root = max(matches, key=lambda candidate: len(candidate.parts))
                deleted_root = storage.check(root / DELETED_IMAGES_FOLDER)
                if deleted_root.exists() and deleted_root.resolve() != deleted_root:
                    raise OSError("已删除图片文件夹指向其他位置，已停止移动")
                target = deleted_root / batch / resolved.relative_to(root)
                # Keep images on the original library's disk. rename is atomic
                # and fails rather than copying/deleting across filesystems.
                if not target.resolve().is_relative_to(deleted_root):
                    raise OSError("保留位置超出已删除图片文件夹")
                data_directory(target.parent).ensure(parents=True, exist_ok=True)
                target = storage.check(target)
                if not target.resolve().is_relative_to(deleted_root):
                    raise OSError("保留位置超出已删除图片文件夹")
                if target.exists():
                    raise FileExistsError("保留位置已存在同名文件，原图未移动")
                data_io.rename(path, target)
                moved.append({"source": raw, "target": str(target)})
            deleted.append(raw)
        except Exception as error:
            failures.append(f"{raw}：{error}")
            failure_reasons.append(str(error))
            logging.getLogger(__name__).warning("图片删除失败 (%s)：%s：%s",
                "永久删除" if permanent else "移入保留文件夹", raw, error)
        operation = "永久删除" if permanent else "移入已删除图片文件夹"
        progress(number, len(paths), f"删除图片 {number}/{len(paths)}，已{operation} {len(deleted)} 张")
    return {"kind": "delete", "deleted": deleted, "failures": failures,
            "cancelled": bool(cancelled()), "permanent": bool(permanent),
            "failure_reasons": failure_reasons, "moved": moved}
