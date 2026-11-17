"""注入到云端沙箱执行的绘图与中文字体初始化引导代码（绝不在 AstrBot 宿主机侧下载字体）。"""

FONT_SETUP_CODE = r'''
import os
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

# Agg 无头后端默认的 show() 不会输出 Notebook 图像；通过 E2B 提供的 display()
# 将所有打开的画布序列化为原始 PNG MIME 数据包回传，并在投递后关闭画布（直接覆盖赋值，天然幂等不递归）。
if callable(globals().get('display')):
    def _e2b_show_figures(*args, **kwargs):
        import base64
        import io
        for number in list(plt.get_fignums()):
            with io.BytesIO() as image:
                plt.figure(number).savefig(image, format='png', bbox_inches='tight')
                display({'image/png': base64.b64encode(image.getvalue()).decode('ascii')}, raw=True)
        plt.close('all')
    plt.show = _e2b_show_figures

def _e2b_configure_font():
    import os
    import sys
    import tempfile
    import time
    import urllib.request
    from matplotlib.ft2font import FT2Font

    # 同一持久沙箱内核若已完成验证且字体文件仍在磁盘上，直接复用内存态免重复解析
    verified_path = getattr(fm, '_e2b_verified_font', '')
    if isinstance(verified_path, str) and verified_path and os.path.exists(verified_path) and plt.rcParams.get('font.sans-serif'):
        return

    def activate(path):
        face = FT2Font(path)
        if not face.get_char_index(ord('中')):
            raise ValueError('font has no Chinese glyphs')
        family = face.family_name
        del face
        if not any(entry.fname == path for entry in fm.fontManager.ttflist):
            fm.fontManager.addfont(path)
        plt.rcParams['font.sans-serif'] = [family, 'DejaVu Sans']
        plt.rcParams['axes.unicode_minus'] = False
        fm._e2b_verified_font = path

    # 优先激活自定义模板预装的中文字体，避免每次执行都发起下载
    for family in ('Noto Sans CJK SC', 'Noto Sans SC', 'WenQuanYi Zen Hei'):
        try:
            path = fm.findfont(fm.FontProperties(family=family), fallback_to_default=False)
            activate(path)
            return
        except Exception:
            pass

    cache_dir = '/tmp/astrbot-fonts'
    font_path = cache_dir + '/NotoSansCJKsc-Regular.otf'
    temp_path = None
    try:
        os.makedirs(cache_dir, exist_ok=True)
        if os.path.exists(font_path):
            try:
                activate(font_path)
                return
            except Exception:
                os.unlink(font_path)

        # Noto CJK Sans 2.004（SIL Open Font License 1.1）
        url = ('https://raw.githubusercontent.com/notofonts/noto-cjk/'
               'Sans2.004/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf')
        limit = 24 * 1024 * 1024
        deadline = time.monotonic() + 20
        with urllib.request.urlopen(url, timeout=10) as response:
            if int(response.headers.get('Content-Length', '0')) > limit:
                raise ValueError('font download is too large')
            with tempfile.NamedTemporaryFile(dir=cache_dir, suffix='.otf', delete=False) as output:
                temp_path = output.name
                total = 0
                read = getattr(response, 'read1', response.read)
                while True:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('font download timed out')
                    chunk = read(min(64 * 1024, limit + 1 - total))
                    if time.monotonic() >= deadline:
                        raise TimeoutError('font download timed out')
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise ValueError('font download is too large')
                    output.write(chunk)
        if total < 1024:
            raise ValueError('font download is too small')
        with open(temp_path, 'rb') as font_file:
            if font_file.read(4) not in (b'OTTO', b'\x00\x01\x00\x00', b'ttcf'):
                raise ValueError('download is not a font')
        face = FT2Font(temp_path)
        if not face.get_char_index(ord('中')):
            raise ValueError('downloaded font has no Chinese glyphs')
        del face
        os.replace(temp_path, font_path)
        temp_path = None
        activate(font_path)
    except Exception as exc:
        print('[E2B] Chinese font setup failed; will retry next execution: ' + str(exc), file=sys.stderr)
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError as exc:
                print('[E2B] Failed to clean up temporary font: ' + str(exc), file=sys.stderr)

_e2b_configure_font()
del _e2b_configure_font
'''
