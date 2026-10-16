"""Per-submission project inputs, never a file binding stored in a node."""
import copy

NAMESPACE = 'batch_text_video'
POOL_KEYS = ('copy_pool','image_pool','backgrounds','music','resource_pools')


def project_payload(state):
    profile_id = state.get('active_profile','')
    profile = next((p.get('name','') for p in state.get('profiles',[]) if p.get('id') == profile_id),'当前项目')
    return {'profile_id':profile_id,'name':profile,
            'data':{key:copy.deepcopy(state.get(key,[])) for key in POOL_KEYS}}


def project_runtime(services):
    provider = getattr(services,'workflow_runtime_context',None)
    if not callable(provider):
        raise ValueError('本次运行没有传入文案视频项目资源。请由文案视频提交，或从主程序节点面板运行；节点不读取旧项目路径。')
    value = provider(NAMESPACE)
    if not isinstance(value,dict) or not isinstance(value.get('data'),dict):
        raise ValueError('本次传入的文案视频资源上下文无效。')
    return copy.deepcopy(value)


class ProjectServices:
    """Submission-local overlay; unrelated host services remain available."""
    def __init__(self, services, state):
        self._services, self._project = services, project_payload(state)

    def workflow_runtime_context(self, namespace):
        if namespace == NAMESPACE: return copy.deepcopy(self._project)
        return self._services.workflow_runtime_context(namespace)

    def __getattr__(self,name):
        return getattr(self._services,name)


def unbind_project_paths(graph):
    """Small in-memory cleanup for existing effect graphs, without disk writes."""
    graph = copy.deepcopy(graph)
    for node in graph.get('nodes',[]):
        if node.get('kind') in {'batch_text_video.load_pool','batch_text_video.component_source'}:
            for key in ('store_file','profile_id'): node['params'].pop(key,None)
    return graph
