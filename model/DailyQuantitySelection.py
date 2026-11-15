"""Explicit selected-cell submission, independent of automatic reconciliation.

Resolve by date/period/creator/category each time; never trust Qt row numbers or
cached A1 positions. Only values are written. No format/validation/formula writes,
no Drive scan, no auto-write setting changes, no automatic write retries.
"""
from datetime import datetime
from collections import Counter
from model import DailyQuantityStats as stats
from model.GoogleSheetsHelper import column_to_letter,sheet_range,load_sheets_service


def _cells(cells):
    if not isinstance(cells,list) or not 1<=len(cells)<=1000:raise ValueError('请选择1至1000个时段数量格。')
    result={}
    for item in cells:
        if not isinstance(item,dict):raise ValueError('所选数量信息无效。')
        day=item.get('date');slot=item.get('slot');number=item.get('count')
        sheet=item.get('sheet');category=item.get('category')
        if not isinstance(day,str) or stats._date(day)!=day or slot not in ('01','02','03'):
            raise ValueError('所选单元格缺少明确的日期或时段；合计不直接提交。')
        stats._reject_future_date(day)
        if not isinstance(sheet,str) or not sheet.strip() or not isinstance(category,str) or not category.strip():
            raise ValueError('所选单元格缺少统计分页或类别。')
        if type(number) is not int or not 0<=number<=2**53-1:raise ValueError('数量必须为可精确保存的非负整数。')
        key=(sheet,day,slot,stats._key(category))
        value={'sheet':sheet,'date':day,'slot':slot,'category':category,'count':number}
        if key in result and result[key]['count']!=number:raise ValueError('同一个数量格被选择了不同数值。')
        result[key]=value
    return list(result.values())


def _batch(api,identifier,ranges,check,**options):
    if not ranges:return []
    check()
    response=api.spreadsheets().values().batchGet(spreadsheetId=identifier,ranges=ranges,**options).execute()
    values=response.get('valueRanges',[])
    if not isinstance(values,list) or len(values)!=len(ranges):raise ValueError('表格读取不完整，未猜测对应单元格。')
    return values


def _raw(api,identifier,ranges,check):
    values=_batch(api,identifier,ranges,check,valueRenderOption='FORMULA')
    return [stats._value(item.get('values',[]),0,0) for item in values]


def _service(config,service):
    if service is not None:return service
    try:
        api=load_sheets_service(config,'task_submission_sheet')
        # This loader creates a service per call. Bound the legacy transport
        # too; managed-account transports already have this same timeout.
        import httplib2
        transport=getattr(api,'_http',None);transport=getattr(transport,'http',transport)
        if isinstance(transport,httplib2.Http):transport.timeout=30
        return api
    except SystemExit as error:raise ValueError(str(error)) from None


def _resolve(api,identifier,creator,cells,check):
    check()
    metadata=api.spreadsheets().get(spreadsheetId=identifier,
        fields='sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))').execute()
    sizes={sheet['properties']['title']:sheet['properties'] for sheet in metadata.get('sheets',[])}
    names=sorted({cell['sheet'] for cell in cells if cell['sheet'] in sizes})
    labels=_batch(api,identifier,[sheet_range(name,'A1:C'+str(sizes[name].get('gridProperties',{}).get('rowCount',1)))
        for name in names],check,valueRenderOption='FORMATTED_VALUE')
    headers=_batch(api,identifier,[sheet_range(name,'A1:'+column_to_letter(sizes[name].get('gridProperties',{}).get('columnCount',1))+'2')
        for name in names],check,valueRenderOption='UNFORMATTED_VALUE',dateTimeRenderOption='SERIAL_NUMBER')
    labels={name:item.get('values',[]) for name,item in zip(names,labels)}
    headers={name:stats._period_columns(item.get('values',[])) for name,item in zip(names,headers)}
    located=[];skipped=[]
    for cell in cells:
        try:
            name=cell['sheet']
            if name not in sizes:raise ValueError('统计分页不存在。')
            if type(sizes[name].get('sheetId')) is not int:raise ValueError('分页身份信息不完整，未猜测。')
            row,col=stats._find_cell([[],[]],cell['date'],cell['slot'],creator,cell['category'],labels[name],headers[name])
            located.append({**cell,'range':sheet_range(name,f'{column_to_letter(col+1)}{row+1}'),'sheet_id':sizes[name].get('sheetId')})
        except (ValueError,TypeError) as error:skipped.append({**cell,'reason':str(error)})
    collisions=Counter(item['range'] for item in located)
    for item in located:
        if collisions[item['range']]>1:skipped.append({**item,'reason':'多个所选时段／类别指向同一个云端格，未猜测覆盖。'})
    located=[item for item in located if collisions[item['range']]==1]
    raw_values=_raw(api,identifier,[cell['range'] for cell in located],check)
    valid=[]
    for cell,raw in zip(located,raw_values):
        try:
            old=stats._number(raw)
            valid.append({**cell,'raw':raw,'before':old})
        except (ValueError,TypeError,OverflowError):skipped.append({**cell,'reason':'目标格有公式或不是非负整数，不覆盖。'})
    return valid,skipped


def prepare_selection(config,root,cells,*,service=None,check=lambda:None):
    requested=_cells(cells);scope=stats._scope_key(config,root)
    creator=str(config.get('task_submission_creator') or '').strip()
    if not creator:raise ValueError('请先设置每日数量表中的制作人。')
    api=_service(config,service)
    with stats._LOCK:
        valid,skipped=_resolve(api,scope.split('|',1)[0],creator,requested,check)
    return {'scope':scope,'creator':creator,'cells':valid,'skipped':skipped,
            'prepared_at':datetime.now().astimezone().isoformat(timespec='seconds')}


def commit_selection(config,root,plan,*,service=None,state_path=None,check=lambda:None):
    scope=stats._scope_key(config,root);creator=str(config.get('task_submission_creator') or '').strip()
    if not isinstance(plan,dict) or plan.get('scope')!=scope or plan.get('creator')!=creator or not creator:
        raise ValueError('任务目录、表格或制作人已改变，请重新选择并提交；未写入旧范围。')
    requested=_cells(plan.get('cells'));api=_service(config,service)
    identifier=scope.split('|',1)[0]
    with stats._LOCK:
        current,skipped=_resolve(api,identifier,creator,requested,check)
        skipped=[*plan.get('skipped',[]),*skipped]
        expected={(item['sheet'],item['date'],item['slot'],item['category']):item for item in plan['cells']}
        approved=[]
        for item in current:
            prior=expected[(item['sheet'],item['date'],item['slot'],item['category'])]
            if item['range']!=prior.get('range') or item['sheet_id']!=prior.get('sheet_id') or item['raw']!=prior.get('raw'):
                skipped.append({**item,'reason':'云端位置或原值在确认后改变，未覆盖；请重新提交该格。'})
            else:approved.append(item)
        updates=[{'range':item['range'],'values':[[item['count']]]} for item in approved if item['before']!=item['count']]
        warnings=[];write_attempted=False
        if updates:
            check();write_attempted=True
            try:
                # values.batchUpdate is not a server-side compare-and-swap.
                # Revalidation minimizes the window; the user's confirmation
                # authorizes only these absolute values, not any broader range.
                api.spreadsheets().values().batchUpdate(spreadsheetId=identifier,
                    body={'valueInputOption':'RAW','data':updates}).execute()
            except Exception as error:
                warnings.append('写入响应异常（'+type(error).__name__+'），未自动重发；正在只读核对实际结果。')
        verified=[];unknown=[]
        try:
            # No cancellation after a write attempt: report the actual outcome
            # instead of claiming cancellation means no cells were changed.
            read_check=(lambda:None) if write_attempted else check
            values=_raw(api,identifier,[item['range'] for item in approved],read_check)
            for item,raw in zip(approved,values):
                try:equal=stats._number(raw)==item['count']
                except (ValueError,TypeError,OverflowError):equal=False
                (verified if equal else unknown).append(item)
        except Exception as error:
            unknown=approved;warnings.append('提交后核对失败（'+type(error).__name__+'），请核对所选格，未自动再次填写。')
        changed=[item for item in verified if item['before']!=item['count']]
        if verified:
            try:
                path=state_path or stats.STATE_FILE;state=stats._load_state(path,strict=True)
                previous=state.get(scope,{})
                entry={'at':datetime.now().astimezone().isoformat(timespec='seconds'),'creator':creator,
                    'cells':[{key:item[key] for key in ('sheet','date','slot','category','range','count','before')} for item in verified]}
                state[scope]={**previous,'selected_submissions':[*previous.get('selected_submissions',[]),entry][-100:]}
                # Do not advance whole-date sync progress or alter automatic
                # writer ownership/base counts for a partial explicit action.
                stats._save_state(path,state)
            except Exception as error:warnings.append('云端已核对成功，但本地提交记录保存失败（'+type(error).__name__+'）。')
        return {'verified':verified,'updated':changed,'unchanged':len(verified)-len(changed),
                'skipped':skipped,'unknown':unknown,'warnings':warnings,'scope':scope,'creator':creator}


def run_selection(payload,context):
    context.check_canceled()
    if payload['action']=='prepare':
        return {'action':'prepare','plan':prepare_selection(payload['config'],payload['root'],payload['cells'],check=context.check_canceled)}
    if payload['action']=='commit':
        return {'action':'commit',**commit_selection(payload['config'],payload['root'],payload['plan'],check=context.check_canceled)}
    raise ValueError('未知的所选数量操作。')
