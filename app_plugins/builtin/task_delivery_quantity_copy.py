"""Read-only, source-ordered one-day grids with spreadsheet-safe TSV copy."""
import csv,io
from qt_compat import QtCore,QtGui,QtWidgets
from model.ClipboardHelper import set_internal_clipboard_text
from model.GoogleSheetsHelper import column_to_letter


class CopyGrid(QtWidgets.QTableWidget):
    copy_failed = QtCore.pyqtSignal(str)
    def keyPressEvent(self,event):
        if event.matches(QtGui.QKeySequence.StandardKey.Copy):self.copy_selection();event.accept();return
        super().keyPressEvent(event)

    def copy_selection(self,numbers=False):
        indexes=self.selectedIndexes()
        if numbers:rows=list(range(self.rowCount()));cols=list(range(3,self.columnCount()))
        elif indexes:
            rows=sorted({i.row() for i in indexes});cols=sorted({i.column() for i in indexes})
            if len(indexes)!=len(rows)*len(cols):
                self.copy_failed.emit('请选择一个连续矩形区域，避免粘贴错位。');return False
        else:self.copy_failed.emit('请先选择要复制的单元格，或点击“复制本分页数量”。');return False
        if not rows or not cols:return False
        source_rows=[self.item(row,3).data(QtCore.Qt.ItemDataRole.UserRole) for row in rows]
        if any(v is None for v in source_rows) or source_rows!=list(range(source_rows[0],source_rows[0]+len(source_rows))):
            self.copy_failed.emit('这些分类在原表中不连续，请分段选择后复制；未生成可能错位的数据。');return False
        if rows!=list(range(rows[0],rows[-1]+1)) or cols!=list(range(cols[0],cols[-1]+1)):
            self.copy_failed.emit('请选择连续的行和列。');return False
        source_cols=[self.item(rows[0],c).data(QtCore.Qt.ItemDataRole.UserRole+2) for c in cols]
        if source_cols!=list(range(source_cols[0],source_cols[0]+len(source_cols))):
            self.copy_failed.emit('所选列在原表中不连续，请只复制连续的数量列，避免覆盖定额或合计公式。');return False
        for row in rows:
            if any(not self.item(row,c).data(QtCore.Qt.ItemDataRole.UserRole+1) for c in cols if c>=3):
                self.copy_failed.emit('选择中有重复或未匹配的分类，请先核对该行。');return False
        stream=io.StringIO(newline='');writer=csv.writer(stream,delimiter='\t',lineterminator='\n')
        def value(r,c):
            text=self.item(r,c).text()
            return "'"+text if c<3 and text.lstrip().startswith(('=','+','-','@')) else text
        writer.writerows([[value(r,c) for c in cols] for r in rows])
        set_internal_clipboard_text(stream.getvalue().rstrip('\n'))
        self.copy_failed.emit(f'已复制 {len(rows)} 行 × {len(cols)} 列（不含表头），请粘贴到原表对应日期和本人区块。')
        return True


class DailyQuantityCopyPanel(QtWidgets.QWidget):
    def __init__(self,parent=None):
        super().__init__(parent);self.results={};self.day=''
        layout=QtWidgets.QVBoxLayout(self);bar=QtWidgets.QHBoxLayout()
        self.sheet=QtWidgets.QComboBox();self.sheet.currentIndexChanged.connect(self.render)
        bar.addWidget(QtWidgets.QLabel('统计分页'));bar.addWidget(self.sheet,1)
        copy=QtWidgets.QPushButton('复制本分页数量');copy.clicked.connect(lambda:self.table.copy_selection(numbers=True));bar.addWidget(copy)
        layout.addLayout(bar)
        self.note=QtWidgets.QLabel('暂无缓存，请点击上方“保存并刷新数量”生成所选日期填写表。');self.note.setWordWrap(True);layout.addWidget(self.note)
        self.table=CopyGrid(0,6);self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectItems)
        self.table.setAlternatingRowColors(True);self.table.horizontalHeader().setStretchLastSection(False)
        for column in (3,4,5):self.table.horizontalHeader().setSectionResizeMode(column,QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.copy_failed.connect(self.note.setText)
        self.table.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.menu)
        layout.addWidget(self.table,1)

    def menu(self,pos):
        menu=QtWidgets.QMenu(self);menu.addAction('复制选中区域\tCtrl+C',self.table.copy_selection)
        menu.addAction('复制本分页数量（不含表头）',lambda:self.table.copy_selection(numbers=True))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def clear_scope(self):self.results={};self.set_day(self.day)

    def show_result(self,result):
        self.results.update(result.get('preview_days',{}));self.set_day(self.day)

    def set_day(self,day):
        self.day=day;selected=self.sheet.currentText()
        blocker=QtCore.QSignalBlocker(self.sheet);self.sheet.clear()
        self.sheet.addItems([t['sheet'] for t in self.results.get(day,{}).get('tables',[])])
        match=self.sheet.findText(selected)
        if match>=0:self.sheet.setCurrentIndex(match)
        del blocker;self.render()

    def render(self):
        preview=self.results.get(self.day,{})
        tables=preview.get('tables',[]);index=self.sheet.currentIndex()
        self.table.clearContents();self.table.setRowCount(0)
        self.table.setHorizontalHeaderLabels(['分组','制作人','统计类别','12点','18点','24点'])
        if not preview:
            self.note.setText(f'{self.day} 尚无填写表缓存。点击“保存并刷新数量”统计一次；切换日期不会自动联网或重新统计。');return
        if not 0<=index<len(tables):
            self.note.setText(f'{self.day} 暂无匹配的本人模板区块，请核对制作人及目标表格；原统计结果保留在概览。');return
        data=tables[index]
        letters=[column_to_letter(c) for c in data['source_columns']]
        self.table.setHorizontalHeaderLabels([*data.get('label_headers',['分组','制作人','统计类别']),*[f'{h}（{c}列）' for h,c in zip(data['headers'],letters)]])
        self.table.setRowCount(len(data['rows']))
        for r,row in enumerate(data['rows']):
            self.table.setVerticalHeaderItem(r,QtWidgets.QTableWidgetItem(str(row['source_row'])))
            for c,value in enumerate([*row['labels'],*row['values']]):
                item=QtWidgets.QTableWidgetItem(str(value));item.setData(QtCore.Qt.ItemDataRole.UserRole,row['source_row'])
                item.setData(QtCore.Qt.ItemDataRole.UserRole+1,row['copyable'])
                item.setData(QtCore.Qt.ItemDataRole.UserRole+2,c+1 if c<3 else data['source_columns'][c-3])
                if c>=3:item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                item.setToolTip(f'原表第{row["source_row"]}行；{row["labels"][2]}')
                self.table.setItem(r,c,item)
        self.table.setColumnWidth(0,85);self.table.setColumnWidth(1,100);self.table.setColumnWidth(2,300)
        warning='；有待核对提示，请查看设置与记录' if preview.get('warnings') else ''
        template='；所选日期已不在云端表头，列顺序采用'+data['template_day']+'模板' if data['template_day']!=self.day else ''
        self.note.setText(f'{self.day} · 缓存：{preview.get("cached_at","")} · 原表分类顺序，左侧行号对应云端行。默认只复制数量，不含表头{warning}{template}。原表结构变动后请刷新。')
