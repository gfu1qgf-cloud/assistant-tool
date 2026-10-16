"""Named data snapshots; explicit code nodes store text, never live callbacks."""

from model.AppDataStorage import data_io, data_file, data_directory
import copy
import json
from pathlib import Path
import re
from uuid import uuid4

from app_paths import DATA_ROOT as APP_ROOT


class WorkflowLibrary:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else APP_ROOT / 'NodeWorkflows'

    def _path(self, key):
        if not isinstance(key, str) or not re.fullmatch(r'[a-f0-9]{32}', key):
            raise ValueError('工作流编号无效。')
        return self.root / (key + '.json')

    def load(self, key, registry=None):
        from .graph import load_graph
        graph = load_graph(self._path(key))
        if graph.get('workflow_id') != key:
            raise ValueError('工作流文件与编号不一致。')
        name = graph.get('workflow_name', '')
        if not isinstance(name, str) or len(name) > 120 or any(ord(ch) < 32 for ch in name):
            raise ValueError('工作流名称格式无效。')
        if registry is not None:
            from .workflow_types import upgrade_workflow
            graph = upgrade_workflow(graph, registry)
        return graph

    def save(self, graph, name, registry):
        from .graph import check_document, save_graph
        name = str(name).strip()
        if not name or len(name) > 120 or any(ord(ch) < 32 for ch in name):
            raise ValueError('请填写不超过120字的工作流名称。')
        # Saving a draft must not require complete ports/files or enabled models.
        # Execution still performs full validation before preparing any job.
        check_document(graph)
        value = copy.deepcopy(graph)
        key = value.get('workflow_id') or uuid4().hex
        target = self._path(key)
        value.update(workflow_id=key, workflow_name=name)
        if len(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')) > 1024 * 1024:
            raise ValueError('工作流超过 1MB，请减少节点参数内容；原存档未修改。')
        data_directory(self.root).ensure(parents=True, exist_ok=True)
        save_graph(target, value)
        return value

    def list(self, type_id=None):
        rows = []
        if not self.root.is_dir():
            return rows
        # One malformed user file must not hide all other workflows.
        for path in self.root.glob('*.json'):
            if not re.fullmatch(r'[a-f0-9]{32}', path.stem):
                continue
            try:
                graph = self.load(path.stem)
                if type_id is not None and graph.get('workflow_type', '') != type_id:
                    continue
                rows.append({'id': path.stem, 'name': graph.get('workflow_name') or path.stem,
                             'type': graph.get('workflow_type', ''), 'path': str(path)})
            except (OSError, ValueError, TypeError):
                continue
        return sorted(rows, key=lambda row: (row['name'].casefold(), row['id']))
