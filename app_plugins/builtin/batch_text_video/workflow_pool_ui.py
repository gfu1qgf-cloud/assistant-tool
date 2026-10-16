"""Choose a pool from the runtime project; no project file/config binding."""
from qt_compat import QtWidgets
from .workflow_pools import pool_records
from .project_runtime import project_runtime


class PoolNodeEditor(QtWidgets.QWidget):
    def __init__(self, params, callback, parent=None):
        super().__init__(parent)
        self.params = {'pool_id':params.get('pool_id','builtin:copy')}
        self.callback, self.loading = callback, False
        self.services = getattr(parent,'workflow_services',None)
        root = QtWidgets.QFormLayout(self)
        root.setContentsMargins(0,0,0,0)
        root.setRowWrapPolicy(root.RowWrapPolicy.WrapLongRows)
        self.origin = QtWidgets.QLabel('来源由本次运行传入')
        self.origin.setWordWrap(True); root.addRow('运行项目',self.origin)
        self.pools = QtWidgets.QComboBox(); root.addRow('资源池',self.pools)
        self.info = QtWidgets.QLabel(); self.info.setWordWrap(True); root.addRow(self.info)
        refresh = QtWidgets.QPushButton('刷新当前项目的资源池')
        refresh.clicked.connect(self.refresh); root.addRow(refresh)
        note = QtWidgets.QLabel('节点只保存资源池选择，不保存项目路径或配置编号。另存项目后自动使用本次运行的项目；预览不推进资源池。')
        note.setWordWrap(True); root.addRow(note)
        self.pools.currentIndexChanged.connect(self.pool_changed)
        self.refresh()

    def refresh(self):
        self.loading = True
        try:
            project = project_runtime(self.services)
            self.origin.setText('当前文案视频项目 · '+project.get('name',''))
            self.pools.clear()
            for pool in pool_records(project['data']):
                self.pools.addItem(f"{pool['name']} · {pool['kind']} · {len(pool['items'])}项",pool['id'])
            wanted = self.params['pool_id']
            if self.pools.findData(wanted) < 0: self.pools.addItem('当前项目暂无此池（请选择）',wanted)
            self.pools.setCurrentIndex(self.pools.findData(wanted))
            self.info.setText('显示当前项目已提供的资源；正式提交时重新获取独立快照。')
        except Exception as error:
            self.info.setText(str(error))
        finally:
            self.loading = False

    def pool_changed(self,*_):
        if not self.loading:
            self.params['pool_id'] = self.pools.currentData()
            try: self.callback(dict(self.params))
            except Exception as error: self.info.setText('设置未应用：'+str(error))


def pool_inspector(params,changed,parent=None):
    return PoolNodeEditor(params,changed,parent)
