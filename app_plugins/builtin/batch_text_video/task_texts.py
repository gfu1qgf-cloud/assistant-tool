"""Read task-table copy without editing the registration workbook."""
from pathlib import Path
from .matching import folder_entry, file_identity


def split_title_body(value):
    lines = str(value or "").replace("\r\n","\n").replace("\r","\n").split("\n")
    title = lines[0].strip().lstrip("\ufeff")
    if not title:
        raise ValueError("第一行没有标题，请在表格中补上标题。")
    body = "\n".join(lines[1:]).strip()
    if not body:
        raise ValueError("只有一行文字，无法确认标题与正文，请按第一行标题、后续正文填写。")
    return title,body


def record_from_task(task, target_dir, source_sheet=""):
    return {"task_id":str(getattr(task,"task_id","") or ""),
            "task_type":str(getattr(task,"task_type","") or ""),
            "task_name":str(getattr(task,"_full_task_name","") or ""),
            "task_audio_text":str(getattr(task,"task_audio_text","") or ""),
            "target_dir":str(target_dir),"source_sheet":str(source_sheet),
            "source_row":getattr(task,"source_row",None)}


def read_sheet_records(path):
    from model.OdsHelper import ReadTaskOds2
    from model.TaskReferenceDownloader import task_directory_for
    from odf import teletype
    from odf.opendocument import load
    from odf.table import Table,TableRow,TableCell
    from odf.text import P
    path = Path(path).resolve()
    before = path.stat()
    tasks,report = ReadTaskOds2(path,return_report=True)
    records = [record_from_task(task,task_directory_for(path.parent,task.task_type,task.task_id),path)
               for task in tasks]
    # Generic task loading flattens some ODS soft line breaks. Copy rendering
    # needs those breaks, including blank first lines, without changing the host.
    document = load(str(path))
    sheet = next(sheet for sheet in document.getElementsByType(Table)
                 if sheet.getAttribute("name") == report["sheet_name"])
    rows = sheet.getElementsByType(TableRow)
    for record in records:
        row = rows[record["source_row"]-1]
        values = []
        for cell in row.getElementsByType(TableCell):
            repeat = min(int(cell.getAttribute("numbercolumnsrepeated") or 1),512-len(values))
            text = "\n".join(teletype.extractText(p) for p in cell.getElementsByType(P))
            values.extend([text]*max(0,repeat))
            if len(values) >= 512:
                break
        for field in ("task_name","task_audio_text"):
            for header in report["matched_headers"].get(field,[]):
                columns = [i for i,value in enumerate(report["headers"]) if value == header]
                source = next((values[column] for column in columns
                               if column < len(values) and values[column].strip()),None)
                if source is not None:
                    record[field] = source
                    break
    after = path.stat()
    if (before.st_mtime_ns,before.st_size) != (after.st_mtime_ns,after.st_size):
        raise ValueError("读取时表格发生了变化，请保存表格后重新导入。")
    return records,report


def entry_from_record(record, source="task_audio_text"):
    title,body = split_title_body(record.get(source,""))
    entry = folder_entry(record["target_dir"],{"text_only":True,"title":title,"body":body,
        "label":record["task_id"],"task_name":record.get("task_name","")})
    entry.update(task_id=record["task_id"],task_type=record["task_type"],
                 source_sheet=record.get("source_sheet",""),source_row=record.get("source_row"),
                 text_source=source,requires_title=True,name=record["task_id"]+"_文字版")
    return entry


def merge_sheet_entries(state, entries):
    """Refresh matching tasks, never duplicate a row or disturb sequence cursors."""
    keys = [file_identity(entry["task_dir"]) for entry in entries]
    if len(set(keys)) != len(keys):
        raise ValueError("选择中有重复的任务类型/编号，请先核对表格，未导入任何任务。")
    added = updated = 0
    for entry,key in zip(entries,keys):
        existing = next((job for job in state["jobs"] if job.get("task_dir")
                         and file_identity(job["task_dir"]) == key),None)
        if existing:
            changed = (existing.get("title"),existing.get("body")) != (entry["title"],entry["body"])
            for field in ("title","body","task_id","task_type","source_sheet","source_row","text_source","requires_title"):
                existing[field] = entry[field]
            if changed:
                existing.update(status="待生成" if existing.get("path") else "需配置",error="")
                updated += 1
        else:
            job = dict(entry)
            pool = state.get("backgrounds",[])
            if not job["path"] and pool:
                job["path"] = pool[(len(state["jobs"])) % len(pool)]
            job.update(status="待生成" if job["path"] else "需配置",match_error="",
                       error="" if job["path"] else "请批量分配背景视频。")
            state["jobs"].append(job)
            added += 1
    return added,updated
