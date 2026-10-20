"""Synthetic, explicit uploaded-reference receipts for offline protocol tests."""
from app_plugins.builtin.flow_assistant.reference_image import freeze_reference


def upload_receipt(service, job_id):
    row = service.store.get(job_id)
    reference = row['remote'].get('upload_reference') or freeze_reference(row, service.downloads_root)
    if not row['remote'].get('upload_reference'):
        service.store.update(job_id, row['status'], remote={**row['remote'], 'upload_reference': reference}, error=row['error'])
    return {'reference': {**reference, 'media_id': 'uploaded_' + row['attempt'], 'verified': True}}


def distinct_jobs(case, count, **options):
    """Parallel-account fixtures use different bytes; same-image tests stay explicit."""
    result = []
    for _ in range(count):
        serial = getattr(case, '_reference_serial', 0) + 1
        case._reference_serial = serial
        image = case.target / f'parallel-reference-{serial}.png'
        image.write_bytes(b'different offline image ' + str(serial).encode())
        result.extend(case.jobs(images=[image], count=1, **options))
    return result
