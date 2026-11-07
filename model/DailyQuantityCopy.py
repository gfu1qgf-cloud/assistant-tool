"""One-date, creator-only copy grids; never issue a cloud write."""
from collections import Counter

AUTO_WRITE_KEY = 'daily_quantity_auto_write'


def auto_write_enabled(config):
    return config.get(AUTO_WRITE_KEY) is True


def valid_preview(value, day):
    try:return _valid_preview(value,day)
    except (TypeError,ValueError,KeyError):return False


def _valid_preview(value, day):
    if not isinstance(value,dict) or value.get('schema')!=1 or value.get('date')!=day:return False
    tables=value.get('tables')
    if not isinstance(tables,list) or len(tables)>100:return False
    for table in tables:
        if not isinstance(table,dict) or not isinstance(table.get('sheet'),str):return False
        if table.get('day')!=day or sorted(table.get('slots',[]))!=['01','02','03']:return False
        cols=table.get('source_columns',[])
        if len(cols)!=3 or any(type(c) is not int or c<1 for c in cols):return False
        if len(table.get('headers',[]))!=3 or not isinstance(table.get('rows'),list):return False
        for row in table['rows']:
            if (not isinstance(row,dict) or type(row.get('source_row')) is not int or row['source_row']<1
                    or len(row.get('labels',[]))!=3 or len(row.get('values',[]))!=3
                    or type(row.get('copyable')) is not bool):return False
            if any(not isinstance(label,str) for label in row['labels']):return False
            if any(v!='' and (type(v) is not int or v<0) for v in row['values']):return False
    return isinstance(value.get('daily_counts'),list) and isinstance(value.get('cached_at'),str)


def make_copy_tables(day, creator, counts, labels, headers, *, owner_rows, periods, normalize):
    tables, warnings = [], []
    lookup = {(row['sheet'], normalize(row['category'])): row for row in counts if row['date'] == day}
    for sheet, rows in labels.items():
        owned = owner_rows(rows, creator)
        if not owned: continue
        columns = periods(headers.get(sheet, []))
        chosen = day
        if any(len(columns.get((chosen, slot), [])) != 1 for slot in ('01','02','03')):
            dates = sorted({d for d, _ in columns}, reverse=True)
            chosen = next((d for d in dates if all(len(columns.get((d, s), [])) == 1 for s in ('01','02','03'))), '')
        if not chosen:
            warnings.append(f'{sheet}：未找到唯一的12/18/24点模板列，未猜测可复制顺序。')
            continue
        slots = sorted(('01','02','03'), key=lambda slot: columns[(chosen,slot)][0])
        cats = Counter(normalize(str(rows[r][2])) for r in owned)
        body = []
        for r in owned:
            label = ['' if v is None else str(v) for v in rows[r][:3]];label += ['']*(3-len(label))
            cat = normalize(str(label[2])); value = lookup.get((sheet,cat), {})
            duplicate = cats[cat] != 1
            if duplicate: warnings.append(f'{sheet} / {label[2]}：本人区块类别重复，不提供猜测数字。')
            body.append({'source_row':r+1,'labels':label,'values':['' if duplicate else int(value.get(s,0)) for s in slots],
                         'copyable':not duplicate})
        tables.append({'sheet':sheet,'day':day,'template_day':chosen,'slots':slots,
            'label_headers':[str(rows[1][i] or default) if len(rows)>1 and len(rows[1])>i else default
                             for i,default in enumerate(('分组','制作人','统计类别'))],
            'source_columns':[columns[(chosen,s)][0]+1 for s in slots],
            'headers':[str(headers[sheet][1][columns[(chosen,s)][0]]) for s in slots], 'rows':body})
    mapped = {(table['sheet'], normalize(str(row['labels'][2]))) for table in tables for row in table['rows']}
    for (sheet,cat), row in lookup.items():
        if (sheet,cat) not in mapped:
            warnings.append(f'{sheet} / {row["category"]}：当前模板的本人区块未匹配到该分类；数量保留在概览，未猜测粘贴位置。')
    return tables, list(dict.fromkeys(warnings))
