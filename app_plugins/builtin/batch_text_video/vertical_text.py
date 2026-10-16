"""Upright, grapheme-safe vertical typography shared by editor and renderer."""
from qt_compat import QtCore, QtGui

WRITING_MODES = {"horizontal", "vertical_rl", "vertical_lr"}
# Display forms only; the user's text, source file and pool entry stay intact.
VERTICAL_FORMS = str.maketrans("，、。；：！？…（）【】《》「」『』",
                             "︐︑︒︔︓︕︖︙︵︶︻︼︽︾﹁﹂﹃﹄")


def graphemes(text):
    """Qt offsets are UTF-16, unlike Python string offsets (emoji/ZWJ)."""
    data = text.encode("utf-16-le", "surrogatepass")
    finder = QtCore.QTextBoundaryFinder(QtCore.QTextBoundaryFinder.BoundaryType.Grapheme, text)
    start, regional = 0, None
    while True:
        end = finder.toNextBoundary()
        if end < 0:
            break
        if end > start:
            value = data[start*2:end*2].decode("utf-16-le", "surrogatepass")
            # Some shipped Qt builds split regional-indicator flag pairs.
            # Pair consecutive indicators without merging separate flags.
            if len(value) == 1 and 0x1F1E6 <= ord(value) <= 0x1F1FF:
                if regional is None:
                    regional = value
                else:
                    yield regional+value
                    regional = None
            else:
                if regional is not None:
                    yield regional
                    regional = None
                yield value
        start = end
    if regional is not None:
        yield regional


class VerticalLayout:
    def __init__(self, placements, width, height, background_rects=None):
        self.placements, self.width, self.height = placements, width, height
        self.background_rects = background_rects or []

    def draw(self, painter, position):
        for layout, x, y in self.placements:
            layout.draw(painter, position + QtCore.QPointF(x, y))


def text_layout(text, family, size, width, height, alignment, spacing, options, scale=1):
    """Height wraps into columns; height=None keeps explicit columns for scrolling."""
    from .layout import _text_layout
    columns, column, used_height, glyph_cache = [], [], 0., {}
    gap = float(options.get("letter_spacing", 0))*scale
    # Shape whole graphemes with the existing font/weight/fallback implementation.
    font_options = {**options, "letter_spacing": 0, "word_spacing": 0}

    def glyph(value):
        shown = value.translate(VERTICAL_FORMS)
        if shown not in glyph_cache:
            groups, glyph_height = _text_layout(shown, family, size, 1000000, "left", 1,
                                                font_options, scale)
            layout = groups[0][0] if groups else None
            glyph_width = layout.lineAt(0).naturalTextWidth() if layout and layout.lineCount() else size
            glyph_cache[shown] = layout, max(1., glyph_width), max(1., glyph_height)
        return glyph_cache[shown]

    _, cell_width, cell_height = glyph("国")

    def finish_column():
        nonlocal column, used_height
        columns.append((column, max(cell_width, max((item[3] for item in column), default=0)),
                        max(cell_height if not column else 0, used_height)))
        column, used_height = [], 0.

    paragraphs = str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for index, paragraph in enumerate(paragraphs):
        if index:
            finish_column()
        for value in graphemes(paragraph):
            layout, gw, gh = glyph(value)
            advance = max(1., max(cell_height, gh))
            extra = gap + (float(options.get("word_spacing", 0))*scale if value.isspace() else 0)
            offset = max(1.-advance, extra) if column else 0
            if height is not None and column and used_height+offset+advance > height+1e-6:
                finish_column()
                offset = 0
            y = used_height+offset
            column.append((layout, value, y, gw))
            used_height = y+advance
    finish_column()
    column_gap = max(0., cell_width*(spacing-1))
    total_width = sum(cw for _, cw, _ in columns)+column_gap*(len(columns)-1)
    total_height = max(ch for _, _, ch in columns)
    # Align the whole block horizontally; shorter columns retain vertical alignment.
    x = max(0., width-total_width)*{"left": 0, "center": .5, "right": 1}[alignment]
    vertical = {"top": 0, "center": .5, "bottom": 1}[options.get("vertical", "top")]
    placements = []
    background_rects = []
    order = reversed(columns) if options.get("writing_mode") == "vertical_rl" else columns
    for cells, cw, ch in order:
        y_offset = (total_height-ch)*vertical
        if any(value.strip() for _layout,value,_y,_gw in cells):
            background_rects.append(QtCore.QRectF(x,y_offset,cw,ch))
        for layout, value, y, gw in cells:
            if layout is not None:
                placements.append((layout, x+(cw-gw)/2, y+y_offset))
        x += cw+column_gap
    block = VerticalLayout(placements, total_width, total_height, background_rects)
    return [(block, 0)], total_height, total_width
