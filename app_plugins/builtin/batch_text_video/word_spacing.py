"""Stable word-row geometry, independent from caption timing and animation."""


def glyph_row_inset(glyphs, row, peak=1, edge_peak=1):
    """Reserve animation bleed at the row edge, not between resting words."""
    return glyphs[row[0]]['width']*max(0,edge_peak-peak)/2 if row else 0


def glyph_row_width(glyphs, row, gap, peak=1, edge_peak=1):
    if not row:
        return 0
    return (sum(glyphs[index]['width']*peak for index in row)+gap*(len(row)-1)
        +(glyphs[row[0]]['width']+glyphs[row[-1]]['width'])*max(0,edge_peak-peak)/2)


def glyph_rows(glyphs, per_line, available_width, gap, peak=1, edge_peak=1):
    """Word count is a maximum; wrap earlier as needed, never split a word."""
    lines, row = [], []
    for index in range(len(glyphs)):
        if row and (len(row) >= per_line or
                glyph_row_width(glyphs,row+[index],gap,peak,edge_peak) > available_width):
            lines.append(row); row=[]
        row.append(index)
    if row: lines.append(row)
    return lines


def natural_space(layer, size):
    from qt_compat import QtGui
    from .fonts import weight_font
    font = weight_font(layer['font'],layer['font_weight'],layer['italic'])
    font.setPixelSize(max(1,round(size)))
    if layer['letter_spacing']:
        font.setLetterSpacing(QtGui.QFont.SpacingType.AbsoluteSpacing,
            layer['letter_spacing']*size/layer['font_max'])
    return QtGui.QFontMetricsF(font).horizontalAdvance(' ')
