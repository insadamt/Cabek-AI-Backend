"""Prepare durable capture attempts without mixing source modes or vehicle phases.

Depends on CaptureService locking; real-camera retries replace the complete phase.
"""

from uuid import uuid4

from api.workflow_service import timestamp


def prepare_capture_attempt(service, dossier, metadata, previous, phase, zoom_channels):
    preferences = service.operation_settings.read() if service.operation_settings else {
        'slow_mode': False, 'demo_mode': service.settings.tunnel_mode == 'mock'}
    modern = (service.settings.tunnel_mode == 'tiandy' or zoom_channels is not None
              or not preferences['demo_mode'] or bool(previous and previous.get('capture_source')))
    mock = getattr(service, 'mock_results', None)
    if mock and mock.read()['enabled']:
        modern = True
    source = ('demo' if preferences['demo_mode'] else 'tiandy') if modern else None
    if mock and mock.read()['enabled']:
        source = service.cameras.source()
    fresh = (dossier.metadata.type == 'forfait_expertise' or source == 'tiandy'
             or bool(previous and previous.get('capture_source') != source)
             or bool(modern and previous and zoom_channels is not None and
                     set(zoom_channels) != set(previous.get('zoom_channels', []))))
    retained = None if fresh else previous
    if retained:
        capture = dict(retained)
        if not modern:
            planned = {sample['source'] for sample in capture['samples']}
            capture['samples'].extend(sample for sample in service.provider.plan_capture(phase)
                                      if sample['source'] not in planned)
    else:
        selected = zoom_channels if zoom_channels is not None else (previous or {}).get('zoom_channels', [])
        if modern:
            source, samples = service.cameras.plan(selected, phase, source)
        else:
            samples = service.provider.plan_capture(phase)
        capture = {'capture_id': uuid4().hex, 'phase': phase, 'samples': samples,
                   'created_at': timestamp(), 'capture_source': source, 'zoom_channels': selected}
        if previous or dossier.metadata.type == 'forfait_expertise':
            service.archive_replaced_photos(metadata, capture['capture_id'], phase)
            service.reset_failed_analysis(dossier)
    capture.update(status='capturing', error_code=None, error_message=None, stage='lighting_preparing',
                   slow_mode=preferences['slow_mode'])
    return capture
