"""Optional progress renewals and quiescent handoff, on the worker thread only."""
import time

CAPABILITY = 'ui_mutex_renew_v1'
STOP_SECONDS = 5
STEPS = {'prepare':{'settings-confirmed'},
         'upload_images':{'reference-mounted'},
         'submit':{'settings-confirmed','reference-mounted','prompt-written','submission-confirmed'}}


def deadline(service, entry):
    from .ui_mutex import phase_limit,host_limit
    return entry.get('expires_at',entry['at']+(host_limit(service) if entry.get('host') else phase_limit(service,entry))-1)


def stop(service, entry, result='expired'):
    """Retain ownership until the exact holder stops or its socket disconnects."""
    from .ui_mutex import client_for
    if not entry.get('stopping'):
        entry.update(stopping=time.time(), stop_result=result)
        client=client_for(service,entry)
        if client:
            client['ui_mutex']={'state':'stopping','operation':entry['operation'],'at':time.time()}
            service.send(client,{'type':'ui_mutex_result','token':entry['token'],'state':'stopping'})
    elif time.time()-entry['stopping']>=STOP_SECONDS and not entry.get('disconnecting'):
        entry['disconnecting']=True
        # abort() terminates this WebSocket, not Chrome or the user's tabs.
        # disconnected() performs the actual handoff; do not just drop owner.
        socket=entry['socket']
        from .service import socket_alive
        if socket_alive(socket):socket.abort()
        service._disconnected(socket)


def renew(service, client, message):
    from .ui_mutex import state,client_for,phase_limit
    from .browser_options import snapshot
    from .setup_support import project_identity
    from .pack_mutex import requests
    entry=state(service).get('owner');now=time.time()
    token=message.get('token');seq=message.get('seq');step=message.get('step')
    mine=bool(entry and not entry.get('host') and entry['token']==token and client_for(service,entry) is client)
    if mine and entry.get('renewable') and now>=deadline(service,entry):
        stop(service,entry)
    elif (mine and entry.get('renewable') and not entry.get('stopping') and not service.closing and
          snapshot(service.preferences)['ui_mutex'] and isinstance(message.get('url'),str) and
          project_identity(message.get('url'))==project_identity(client.get('saved_project')) and
          type(seq) is int and 0<seq<=16 and isinstance(step,str) and step in STEPS.get(entry['operation'],set())):
        previous=entry.get('renew_seq',0)
        # Retransmitted packets return the same receipt, without extending again.
        if seq==previous and step==entry.get('renew_step'):
            pass
        elif seq>previous and step not in entry.get('renew_steps',[]):
            entry.setdefault('renew_steps',[]).append(step)
            entry.update(renew_seq=seq,renew_step=step,
                expires_at=max(deadline(service,entry),now+phase_limit(service,entry)-1))
            for pending in requests(service,entry):pending['at']=now
        else:return
        service.send(client,{'type':'ui_mutex_result','token':token,'state':'granted',
            'renew_seq':seq,'expires_at':deadline(service,entry)})
