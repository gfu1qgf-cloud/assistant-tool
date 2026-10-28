"""Recycle explicitly selected loose-library images without permanent fallback."""
from qt_compat import QtCore
from model.AppDataStorage import application_storage
from model.InventoryManager import MATERIAL_IMAGE_SUFFIXES


def trash_selected(paths, roots, progress, cancelled):
    storage = application_storage()
    roots = [storage.check(root).resolve() for root in roots]
    deleted, failures = [], []
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
            if not any(root in resolved.parents for root in roots):
                raise OSError("图片不在当前选定的图片库内")
            if path.suffix.lower() not in MATERIAL_IMAGE_SUFFIXES:
                raise OSError("不是支持的图片文件")
            file = QtCore.QFile(str(path))
            if not file.moveToTrash():
                raise OSError(file.errorString() or "无法移入系统回收站")
            deleted.append(raw)
        except Exception as error:
            failures.append(f"{raw}：{error}")
        progress(number, len(paths), f"删除图片 {number}/{len(paths)}，已移入回收站 {len(deleted)} 张")
    return {"kind": "delete", "deleted": deleted, "failures": failures,
            "cancelled": bool(cancelled())}
