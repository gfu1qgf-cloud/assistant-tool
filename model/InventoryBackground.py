"""Inventory job runners use snapshots and their own store, never live widgets."""
from pathlib import Path

from model.BackgroundTasks import BackgroundTaskResultError, directory_tree_resource
from model.InventoryManager import InventoryStore, material_directory_stats
from model.MaterialDriveSync import MaterialSyncStateStore, sync_material_drive_folder


def inventory_resources(store):
    return (directory_tree_resource(store.material_root),
            "inventory-state:" + str(store.path.resolve()).casefold())


def material_operation(store, payload, progress):
    operation = payload.get("operation", "material")
    profile = payload.get("profile", {})
    paths = payload.get("source_paths", [])
    item_id = payload.get("material_id")
    if operation == "person_add":
        return store.add_person(profile.get("name"), avatar_path=profile.get("avatar_path", ""),
                                google_sheet_links=profile.get("google_sheet_links", []),
                                source_paths=paths, progress_callback=progress)
    if operation == "person_import":
        return store.import_materials_to_person(item_id, profile.get("material_ids", []),
                    remove_originals=profile.get("remove_originals", False), progress_callback=progress)
    if operation == "person_append":
        return store.append_person_material(item_id, paths, progress_callback=progress)
    if item_id:
        return store.append_material(item_id, paths, progress_callback=progress)
    return store.add_material(payload["name"], paths, progress_callback=progress,
                              use_drive_folder_name=payload.get("use_drive_folder_name", False))


def run_material_job(payload, context):
    store = InventoryStore(payload["state_path"], payload["material_root"])
    context.report("准备复制／下载素材", None)
    result = material_operation(store, payload, context.report)
    paths = [result["path"]] if result.get("path") else []
    return {"item": result, "paths": paths}


def run_material_check_job(payload, context):
    context.report("正在检查素材库目录", None)
    store = InventoryStore(payload["state_path"], payload["material_root"])
    # InventoryStore's check is one transaction. Do not terminate in the
    # middle of its write; a late cancel keeps a coherent completed result.
    return {**store.check_materials(), "paths": []}


def run_material_stats_job(payload, context):
    stats = {}
    for index, path in enumerate(payload["directories"]):
        context.report(f"读取素材目录 {index+1}/{len(payload['directories'])} · {Path(path).name}",
                       int(index*100/max(1, len(payload["directories"]))))
        stats[path] = material_directory_stats(path, cancelled=context.is_canceled)
        context.check_canceled()  # Never publish a canceled, partial size scan.
    return {"stats": stats, "paths": []}


def run_material_sync_job(payload, context):
    # Cancellation is checked in the sync loop. Do not throw from a progress
    # callback after a completed download: its history must still be saved.
    def progress(message):
        if not context.is_canceled():
            context.report(message)
    result = sync_material_drive_folder(payload["settings"],
        state_store=MaterialSyncStateStore(payload["state_path"]),
        force_ids=payload.get("force_ids", []), adopt_existing=payload.get("adopt_existing", False),
        material_root=payload["material_root"], progress_callback=progress, cancelled=context.is_canceled)
    result["material_id"] = payload["settings"].get("material_id", "")
    result["paths"] = list(result.get("downloaded", []))
    if result.get("errors") and not context.is_canceled():
        raise BackgroundTaskResultError("网盘素材部分同步失败：" + "；".join(result["errors"]), result)
    return result
