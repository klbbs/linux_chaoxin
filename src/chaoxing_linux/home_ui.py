"""Overview of the Linux study desk."""
from datetime import date
import math
import subprocess
import sys

from gi.repository import Atk, Gtk


HOME_CSS = """
.desk-heading, .desk-section, .desk-hero-title, .desk-tool-title {
    font-family: "Noto Serif CJK SC", "Source Han Serif SC", serif;
    font-weight: bold;
}
.desk-heading { font-size: 32px; color: #203d32; }
.desk-date { color: #536357; font-size: 13px; }
.desk-hero {
    background-color: #173f34;
    border-radius: 15px;
    padding: 0 22px 0 29px;
}
.desk-hero-title { font-size: 34px; color: #fffbeb; }
.desk-hero-sub { color: #d5e1ce; font-size: 14px; }
button.desk-hero-button {
    color: #183e32; background: #f1e9c8; border: 1px solid #f1e9c8;
    border-radius: 7px; padding: 7px 13px; font-weight: bold; box-shadow: none;
}
button.desk-hero-button:hover { background: #fff9de; }
button.desk-hero-button:active { background: #e6dcac; }
.desk-section { font-size: 21px; color: #243d32; }
button.desk-tool {
    background: #fffefa; color: #263c31; border: 1px solid #d7dbce;
    border-radius: 10px; padding: 13px 15px; box-shadow: none;
}
button.desk-tool:hover { background: #f3f5eb; border-color: #869c87; }
button.desk-tool:active { background: #e5ebdf; }
.desk-tool-title { font-size: 17px; color: #213e31; }
.desk-tool-detail { font-size: 13px; color: #546359; }
.desk-arrow { color: #67776a; }
.desk-stamp { border-radius: 9px; }
.desk-stamp.sage { background-color: #e2ebdf; color: #345b40; }
.desk-stamp.ochre { background-color: #f3e7c8; color: #765c21; }
.desk-stamp.clay { background-color: #f1dfd4; color: #8c4f35; }
.desk-stamp.bluegrey { background-color: #e1e9ec; color: #3b5b68; }
button.desk-minor {
    background: transparent; color: #4e6457; border: 1px solid transparent;
    border-radius: 6px; padding: 4px 6px; box-shadow: none;
}
button.desk-minor:hover { background: #e6ecdf; color: #203c31; }
"""


def _style(widget, name):
    widget.get_style_context().add_class(name)
    return widget


def _text(text, style):
    return _style(Gtk.Label(label=text, xalign=0), style)


def _icon(name, pixels):
    image = Gtk.Image.new_from_icon_name(name, Gtk.IconSize.BUTTON)
    image.set_pixel_size(pixels)
    return image


def _action(title, icon, callback, style, icon_first=False):
    button = _style(Gtk.Button(), style)
    line = Gtk.Box(spacing=8)
    children = [_icon(icon, 15), Gtk.Label(label=title)]
    for widget in children if icon_first else reversed(children):
        line.pack_start(widget, False, False, 0)
    button.add(line)
    button.get_accessible().set_name(title)
    button.connect('clicked', callback)
    return button


def _draw_books(area, cr):
    """Draw a small bookplate in coordinates of the 316 × 188 original."""
    size = area.get_allocation()
    scale = min(size.width / 316, size.height / 188)
    cr.translate(size.width - 316 * scale, (size.height - 188 * scale) / 2)
    cr.scale(scale, scale)

    def ink(rgb):
        cr.set_source_rgb(*(int(rgb[i:i+2], 16) / 255 for i in (0, 2, 4)))

    def finish(fill, edge, width=1):
        ink(fill)
        cr.fill_preserve()
        ink(edge)
        cr.set_line_width(width)
        cr.stroke()

    def polygon(points, fill, edge):
        cr.move_to(*points[0])
        for p in points[1:]:
            cr.line_to(*p)
        cr.close_path()
        finish(fill, edge)

    def stroke(color, width=1):
        ink(color)
        cr.set_line_width(width)
        cr.stroke()

    # A quiet circular stipple behind the books suggests a printer's halftone.
    for y in range(17, 177, 7):
        for x in range(74, 306, 7):
            if ((x-187)/117)**2 + ((y-95)/80)**2 < 1:
                cr.arc(x, y, 0.62, 0, 2 * math.pi)
                cr.new_sub_path()
    ink('6f8b73')
    cr.fill()

    def closed_book(x, y, a, b, height, cover):
        front = (x+a[0], y+a[1])
        back = (x+b[0], y+b[1])
        outer = (front[0]+b[0], front[1]+b[1])
        x1, y1 = front
        x2, y2 = outer
        polygon([(x,y),front,outer,back],cover,'b3c3a0')
        polygon([(x,y),front,(x1,y1+height),(x,y+height)],cover,'b3c3a0')
        polygon([front,outer,(x2,y2+height),(x1,y1+height)],'d0d1ae','abb998')
        for dy in range(3, height, 3):
            cr.move_to(x1+2, y1+dy)
            cr.line_to(x2-2, y2+dy)
        stroke('7c8e75',0.7)
        for step in (0.075, 0.106, 0.89, 0.923):
            cr.move_to(x+a[0]*step, y+a[1]*step+1)
            cr.line_to(x+a[0]*step, y+a[1]*step+height-1)
        stroke('d1c198',1.25)

    closed_book(33, 123, (124, 29), (85, -44), 12, '8a6448')
    closed_book(45, 110, (126, 24), (83, -43), 12, '577866')

    # The layers at the fore edge retain the same Bézier bend as the paper.
    cr.move_to(30,95)
    cr.curve_to(68,84,105,99,141,125)
    cr.curve_to(181,94,213,87,249,99)
    cr.line_to(281,45)
    cr.line_to(281,53)
    cr.line_to(250,107)
    cr.curve_to(211,96,181,105,142,134)
    cr.curve_to(104,109,70,92,31,104)
    cr.close_path()
    finish('a7b399','d0d6bb')
    for offset in (3,6):
        cr.move_to(32,95+offset)
        cr.curve_to(68,84+offset,105,99+offset,141,125+offset)
        cr.curve_to(181,94+offset,213,87+offset,249,99+offset)
        cr.line_to(279,48+offset)
    stroke('687e68',0.7)

    cr.move_to(30,95)
    cr.curve_to(68,84,105,99,141,125)
    cr.line_to(173,70)
    cr.curve_to(138,44,99,28,62,39)
    cr.close_path()
    finish('cdd4b9','f1eed6',1.2)
    cr.move_to(141,125)
    cr.curve_to(181,94,213,87,249,99)
    cr.line_to(281,45)
    cr.curve_to(245,32,213,40,173,70)
    cr.close_path()
    finish('e7e2bd','f5f0d7',1.2)

    for n in range(8):
        dx, dy = 3.05*n,-5.3*n
        cr.move_to(47+dx,91+dy)
        cr.curve_to(72+dx,87+dy,103+dx,99+dy,124+dx,113+dy)
        cr.move_to(159+dx,110+dy)
        cr.curve_to(183+dx,95+dy,212+dx,88+dy,235+dx,95+dy)
    stroke('8c9d83',0.75)
    polygon([(199,101),(205,98),(193,118),(187,117),(181,123)],'c08b54','c08b54')
    return False


def build_home(window, page):
    def go(name):
        return lambda _: window.stack.set_visible_child_name(name)

    header = Gtk.Box(spacing=14)
    header.pack_start(_text('学习工作台', 'desk-heading'), True, True, 0)
    calendar = _style(Gtk.Box(spacing=6), 'desk-date')
    calendar.pack_start(_icon('x-office-calendar-symbolic', 16), False, False, 0)
    date_label = _text('', 'desk-date')

    def display_date(*_):
        today = date.today()
        date_label.set_text(f'{today.month}月{today.day}日  星期{"一二三四五六日"[today.weekday()]}')

    display_date()
    page.connect('map', display_date)
    calendar.pack_start(date_label, False, False, 0)
    header.pack_end(calendar, False, False, 0)
    page.pack_start(header, False, False, 0)

    hero = _style(Gtk.Box(spacing=12), 'desk-hero')
    copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
    copy.set_valign(Gtk.Align.CENTER)
    copy.pack_start(_text('我的课程', 'desk-hero-title'), False, False, 0)
    subtitle = _text('查看课程章节与任务进度', 'desk-hero-sub')
    copy.pack_start(subtitle, False, False, 0)
    enter = _action('进入课程', 'go-next-symbolic', go('courses'), 'desk-hero-button')
    enter.set_halign(Gtk.Align.START)
    enter.set_margin_top(9)
    copy.pack_start(enter, False, False, 0)
    hero.pack_start(copy, True, True, 0)
    art = Gtk.DrawingArea()
    art.set_size_request(190, 196)
    art.set_hexpand(True)
    art.connect('draw', _draw_books)
    art.get_accessible().set_role(Atk.Role.REDUNDANT_OBJECT)
    hero.pack_end(art, True, True, 0)
    page.pack_start(hero, False, False, 0)

    section = _text('常用工具', 'desk-section')
    section.set_margin_top(4)
    page.pack_start(section, False, False, 0)
    tools = Gtk.Grid(column_spacing=12, row_spacing=12, column_homogeneous=True)
    shortcuts = (
        ('作业 AI', '读题、解答与提交', 'document-edit-symbolic', 'sage', 'work'),
        ('复习资料', '生成 Word 与 PDF 讲义', 'folder-documents-symbolic', 'ochre', 'review'),
        ('签到', '查看课程活动与签到', 'mark-location-symbolic', 'clay', 'sign'),
        ('手机控制', '连接手机，操作学习通', 'phone-symbolic', 'bluegrey', 'phone'),
    )
    for index, (name, detail, symbol, color, target) in enumerate(shortcuts):
        tile = _style(Gtk.Button(), 'desk-tool')
        tile.set_hexpand(True)
        line = Gtk.Box(spacing=12)
        stamp = _style(_style(Gtk.Box(), 'desk-stamp'), color)
        stamp.set_size_request(40, 40)
        stamp.set_valign(Gtk.Align.CENTER)
        stamp.pack_start(_icon(symbol, 21), True, True, 0)
        line.pack_start(stamp, False, False, 0)
        copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        copy.pack_start(_text(name, 'desk-tool-title'), False, False, 0)
        copy.pack_start(_text(detail, 'desk-tool-detail'), False, False, 0)
        line.pack_start(copy, True, True, 0)
        line.pack_end(_style(_icon('go-next-symbolic', 14), 'desk-arrow'), False, False, 0)
        tile.add(line)
        tile.get_accessible().set_name(name)
        tile.get_accessible().set_description(detail)
        tile.connect('clicked', go(target))
        tools.attach(tile, index % 2, index // 2, 1, 1)
    page.pack_start(tools, False, False, 0)

    footer = Gtk.Box(spacing=14)
    footer.pack_start(_action('登录与 AI 设置', 'preferences-system-symbolic', go('settings'),
                              'desk-minor', icon_first=True), False, False, 0)
    footer.pack_start(_action('旧兼容环境启动器', 'system-run-symbolic',
                              lambda _: subprocess.Popen([sys.executable, '-m', 'chaoxing_linux', '--legacy-launcher']),
                              'desk-minor', icon_first=True), False, False, 0)
    page.pack_start(footer, False, False, 0)
