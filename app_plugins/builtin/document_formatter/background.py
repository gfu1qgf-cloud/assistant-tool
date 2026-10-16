"""Host-owned jobs; payloads contain plain snapshots, never Qt widgets."""
from model.BackgroundTasks import BackgroundTaskResultError
from .odt import inspect_odt, format_odt
from .google_auth import authorize, load_service
from .google_docs import document_target, inspect_document, apply_document, restore_document


def run_document_job(payload, run):
    operation = payload['operation']; run.check_canceled()
    if operation == 'authorize': return authorize(payload['private_root'], run)
    if operation == 'inspect_odt':
        run.report('读取ODT段落和样式；不修改文件。', 20)
        return inspect_odt(payload['source'])
    if operation in {'format_odt', 'batch_odt'}:
        plans = payload.get('plans', [])
        if operation == 'batch_odt':
            known = {plan['snapshot']['source']: plan for plan in payload['known']}
            plans = [known.get(source, {'source': source}) for source in payload['sources']]
        if not plans: raise ValueError('请先添加至少一份ODT文字文档。')
        results = []
        for index, plan in enumerate(plans):
            source = plan.get('source') or plan['snapshot']['source']
            run.check_canceled(); run.report(f'格式化 {index+1}/{len(plans)}', index*100//len(plans))
            try:
                snapshot = plan.get('snapshot') or inspect_odt(source)
                value = format_odt(snapshot, payload['rules'], plan.get('roles'), run.check_canceled)
                results.append({**value, 'source': source, 'status': 'success'})
            except Exception as error:
                # Cancellation must escape, not be reported as a damaged document.
                run.check_canceled()
                results.append({'source': source, 'status': 'failed', 'error': str(error), 'paths': []})
        failures = sum(item['status'] == 'failed' for item in results)
        value = {'kind': 'batch', 'results': results, 'paths': [path for item in results for path in item['paths']]}
        if failures: raise BackgroundTaskResultError(f'成功{len(results)-failures}，失败{failures}；可查看逐文件结果。', value)
        return value
    run.report('连接Google Docs官方接口；不读取其他文档。', 10)
    service = load_service(payload['private_root'])
    if operation == 'inspect_google':
        document_id, tab_id = document_target(payload['source'])
        return inspect_document(service, document_id, payload.get('tab_id', tab_id))
    if operation == 'format_google':
        run.report('校验修订号并保存恢复记录。', 35)
        return apply_document(service, payload['snapshot'], payload['rules'], payload['roles'], payload['private_root'], run.check_canceled)
    if operation == 'restore_google': return restore_document(service, payload['private_root'], run.check_canceled)
    raise ValueError('未知的文档操作。')
