import base64
import ctypes
from datetime import datetime
from io import BytesIO
import json
import math
import os
from pathlib import Path
import struct
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
import warnings
import pystray
import zlib
from PIL import (
    Image,
    ImageDraw,
    ImageEnhance,
    ImageFilter,
    ImageGrab,
    ImageTk,
)

myappid = "clipstack.app.1.0"
ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)


BACKGROUND = "#1e1e1e"
PANEL = "#2b2b2b"
CARD = "#383838"
CARD_SIZE = 120
CARD_GAP = 12
HOVER_LIFT = 6
MAX_HISTORY = 50
MAX_TEXT_LINES = 4
APP_FONT_FAMILY = "Google Sans"


THEMES = {
    "dark": {
        "background": "#1e1e1e",
        "panel": "#2b2b2b",
        "card": "#383838",
        "text": "#eeeeee",
        "muted": "#a0a0a0",
        "icon": "#b8b8b8",
        "scrollbar": "#555555",
        "scrollbar_hover": "#707070",
    },
    "light": {
        "background": "#f2f2f2",
        "panel": "#ffffff",
        "card": "#e5e5e5",
        "text": "#202020",
        "muted": "#686868",
        "icon": "#555555",
        "scrollbar": "#b8b8b8",
        "scrollbar_hover": "#888888",
    },
}


def theme_settings_path():
    if sys.platform == "win32":
        app_data = Path(
            os.environ.get(
                "LOCALAPPDATA",
                Path.home() / "AppData" / "Local",
            )
        )
        return app_data / "ClipStack" / "settings.json"
    return Path.home() / ".config" / "ClipStack" / "settings.json"


def load_theme():
    settings_path = theme_settings_path()
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "dark"
    except (OSError, json.JSONDecodeError) as error:
        warnings.warn(f"Could not read theme settings: {error}", RuntimeWarning)
        return "dark"

    theme = settings.get("theme") if isinstance(settings, dict) else None
    if not isinstance(theme, str) or theme not in THEMES:
        warnings.warn("Theme settings are invalid; using dark mode.", RuntimeWarning)
        return "dark"
    return theme


def save_theme(theme):
    settings_path = theme_settings_path()
    try:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            json.dumps({"theme": theme}, indent=2),
            encoding="utf-8",
        )
    except OSError as error:
        warnings.warn(f"Could not save theme settings: {error}", RuntimeWarning)


def register_app_font(font_path):
    if sys.platform != "win32":
        return False

    from ctypes import wintypes

    add_font_resource = ctypes.windll.gdi32.AddFontResourceExW
    add_font_resource.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID)
    add_font_resource.restype = wintypes.INT
    if not add_font_resource(str(font_path), 0x10, None):
        raise ctypes.WinError()
    return True


def unregister_app_font(font_path):
    if sys.platform != "win32":
        return

    from ctypes import wintypes

    remove_font_resource = ctypes.windll.gdi32.RemoveFontResourceExW
    remove_font_resource.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID)
    remove_font_resource.restype = wintypes.BOOL
    if not remove_font_resource(str(font_path), 0x10, None):
        warnings.warn("Could not unregister the Google Sans app font.", RuntimeWarning)


def load_history(history_path, master):
    if not history_path.exists():
        history_path.write_text(
            json.dumps({"version": 1, "items": []}, indent=2),
            encoding="utf-8",
        )
        return []

    try:
        stored_history = json.loads(history_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        warnings.warn(f"Could not read clipboard history: {error}", RuntimeWarning)
        return []

    if not isinstance(stored_history, dict) or not isinstance(
        stored_history.get("items"), list
    ):
        warnings.warn("Clipboard history file has an invalid structure.", RuntimeWarning)
        return []

    history = []
    for item in stored_history["items"]:
        try:
            item_type = item["type"]
            copied_at = datetime.fromisoformat(item["timestamp"])
            if item_type == "text":
                content = item["content"]
                if not isinstance(content, str):
                    raise ValueError("Text content must be a string.")
                history.append(("text", content, copied_at, None, None))
            elif item_type == "image":
                image_format = item["format"]
                image_data = base64.b64decode(item["content"], validate=True)
                thumbnail = open_clipboard_image(image_format, image_data, master)
                history.append(
                    ("image", image_data, copied_at, thumbnail, image_format)
                )
            else:
                raise ValueError(f"Unsupported clipboard item type: {item_type!r}.")
        except (
            KeyError,
            TypeError,
            ValueError,
            OSError,
            tk.TclError,
            Image.DecompressionBombError,
        ) as error:
            warnings.warn(f"Skipping invalid clipboard history item: {error}", RuntimeWarning)

    history.sort(key=lambda entry: entry[2], reverse=True)
    return history[:MAX_HISTORY]


def save_history(history_path, history):
    items = []
    for item_type, content, copied_at, _thumbnail, image_format in history:
        if item_type == "image":
            content = base64.b64encode(content).decode("ascii")
        items.append(
            {
                "type": item_type,
                "content": content,
                "timestamp": copied_at.isoformat(),
                **({"format": image_format} if item_type == "image" else {}),
            }
        )

    temporary_path = history_path.with_suffix(history_path.suffix + ".tmp")
    try:
        temporary_path.write_text(
            json.dumps({"version": 1, "items": items}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary_path.replace(history_path)
    except OSError as error:
        warnings.warn(f"Could not save clipboard history: {error}", RuntimeWarning)


def dib_to_rgb(data):
    if len(data) < 40:
        return None

    (
        header_size,
        width,
        signed_height,
        planes,
        bits_per_pixel,
        compression,
        image_size,
        _horizontal_resolution,
        _vertical_resolution,
        _colors_used,
        _important_colors,
    ) = struct.unpack_from("<IiiHHIIiiII", data)
    height = abs(signed_height)
    if (
        header_size < 40
        or width <= 0
        or height <= 0
        or planes != 1
        or bits_per_pixel not in (24, 32)
        or compression not in (0, 3, 6)
    ):
        return None

    pixel_offset = header_size
    red_mask = green_mask = blue_mask = None
    if compression in (3, 6):
        if header_size >= 52:
            red_mask, green_mask, blue_mask = struct.unpack_from("<III", data, 40)
        else:
            mask_count = 4 if compression == 6 else 3
            mask_bytes = mask_count * 4
            if len(data) < pixel_offset + mask_bytes:
                return None
            red_mask, green_mask, blue_mask = struct.unpack_from(
                "<III", data, pixel_offset
            )
            pixel_offset += mask_bytes

    row_stride = ((width * bits_per_pixel + 31) // 32) * 4
    if len(data) < pixel_offset + row_stride * height:
        return None

    scale = min(1, 92 / width, 66 / height)
    output_width = max(1, round(width * scale))
    output_height = max(1, round(height * scale))
    pixels = bytearray()

    def extract_channel(pixel, mask):
        if not mask:
            return 0
        shift = (mask & -mask).bit_length() - 1
        value = (pixel & mask) >> shift
        maximum = mask >> shift
        return (value * 255 + maximum // 2) // maximum

    for output_y in range(output_height):
        source_y = min(height - 1, output_y * height // output_height)
        row_y = source_y if signed_height < 0 else height - 1 - source_y
        row_offset = pixel_offset + row_y * row_stride
        for output_x in range(output_width):
            source_x = min(width - 1, output_x * width // output_width)
            pixel_offset_x = row_offset + source_x * (bits_per_pixel // 8)
            if bits_per_pixel == 24:
                blue, green, red = data[pixel_offset_x : pixel_offset_x + 3]
            elif compression in (3, 6):
                pixel = int.from_bytes(data[pixel_offset_x : pixel_offset_x + 4], "little")
                red = extract_channel(pixel, red_mask)
                green = extract_channel(pixel, green_mask)
                blue = extract_channel(pixel, blue_mask)
            else:
                blue, green, red = data[pixel_offset_x : pixel_offset_x + 3]
            pixels.extend((red, green, blue))

    return output_width, output_height, bytes(pixels)


def png_to_rgb(data):
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None

    width = height = bit_depth = color_type = interlace = None
    compressed = bytearray()
    palette = None
    transparency = b""
    offset = 8
    while offset + 12 <= len(data):
        chunk_length = struct.unpack_from(">I", data, offset)[0]
        chunk_type = data[offset + 4 : offset + 8]
        chunk_start = offset + 8
        chunk_end = chunk_start + chunk_length
        if chunk_end + 4 > len(data):
            return None
        chunk_data = data[chunk_start:chunk_end]
        if chunk_type == b"IHDR":
            if len(chunk_data) != 13:
                return None
            width, height, bit_depth, color_type, compression, filtering, interlace = (
                struct.unpack(">IIBBBBB", chunk_data)
            )
            if compression != 0 or filtering != 0 or interlace != 0:
                return None
        elif chunk_type == b"IDAT":
            compressed.extend(chunk_data)
        elif chunk_type == b"PLTE":
            palette = chunk_data
        elif chunk_type == b"tRNS":
            transparency = chunk_data
        elif chunk_type == b"IEND":
            break
        offset = chunk_end + 4

    channels_by_type = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
    if (
        not width
        or not height
        or width * height > 50_000_000
        or bit_depth != 8
        or color_type not in channels_by_type
    ):
        return None
    channels = channels_by_type[color_type]
    if color_type == 3 and (not palette or len(palette) % 3):
        return None

    try:
        raw = zlib.decompress(compressed)
    except zlib.error:
        return None

    row_bytes = width * channels
    if len(raw) != (row_bytes + 1) * height:
        return None

    scale = min(1, 92 / width, 66 / height)
    output_width = max(1, round(width * scale))
    output_height = max(1, round(height * scale))
    output = bytearray()
    previous = bytearray(row_bytes)
    source_offset = 0
    output_y = 0

    def paeth_predictor(left, above, upper_left):
        estimate = left + above - upper_left
        distances = (
            abs(estimate - left),
            abs(estimate - above),
            abs(estimate - upper_left),
        )
        return (left, above, upper_left)[distances.index(min(distances))]

    for source_y in range(height):
        filter_type = raw[source_offset]
        source_offset += 1
        row = bytearray(raw[source_offset : source_offset + row_bytes])
        source_offset += row_bytes
        if filter_type > 4:
            return None
        for index in range(row_bytes):
            left = row[index - channels] if index >= channels else 0
            above = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = above
            elif filter_type == 3:
                predictor = (left + above) // 2
            elif filter_type == 4:
                predictor = paeth_predictor(left, above, upper_left)
            else:
                predictor = 0
            row[index] = (row[index] + predictor) & 0xFF

        if source_y == output_y * height // output_height:
            for output_x in range(output_width):
                source_x = output_x * width // output_width
                pixel_offset = source_x * channels
                if color_type == 0:
                    red = green = blue = row[pixel_offset]
                    alpha = 255
                elif color_type == 2:
                    red, green, blue = row[pixel_offset : pixel_offset + 3]
                    alpha = 255
                elif color_type == 3:
                    palette_index = row[pixel_offset]
                    palette_offset = palette_index * 3
                    if palette_offset + 3 > len(palette):
                        return None
                    red, green, blue = palette[palette_offset : palette_offset + 3]
                    alpha = (
                        transparency[palette_index]
                        if palette_index < len(transparency)
                        else 255
                    )
                elif color_type == 4:
                    red = green = blue = row[pixel_offset]
                    alpha = row[pixel_offset + 1]
                else:
                    red, green, blue, alpha = row[pixel_offset : pixel_offset + 4]
                if alpha != 255:
                    red = (red * alpha + 56 * (255 - alpha)) // 255
                    green = (green * alpha + 56 * (255 - alpha)) // 255
                    blue = (blue * alpha + 56 * (255 - alpha)) // 255
                output.extend((red, green, blue))
            output_y += 1
        previous = row

    if output_y != output_height:
        return None
    return output_width, output_height, bytes(output)


def read_windows_clipboard_image(window):
    if sys.platform != "win32":
        return False, None

    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    open_clipboard = user32.OpenClipboard
    open_clipboard.argtypes = (wintypes.HWND,)
    open_clipboard.restype = wintypes.BOOL
    if not open_clipboard(window.winfo_id()):
        return False, None

    try:
        register_format = user32.RegisterClipboardFormatW
        register_format.argtypes = (wintypes.LPCWSTR,)
        register_format.restype = wintypes.UINT
        formats = (
            (register_format("PNG"), "png"),
            (17, "dib"),
            (8, "dib"),
        )

        is_available = user32.IsClipboardFormatAvailable
        is_available.argtypes = (wintypes.UINT,)
        is_available.restype = wintypes.BOOL
        get_data = user32.GetClipboardData
        get_data.argtypes = (wintypes.UINT,)
        get_data.restype = wintypes.HANDLE
        global_size = kernel32.GlobalSize
        global_size.argtypes = (wintypes.HGLOBAL,)
        global_size.restype = ctypes.c_size_t
        global_lock = kernel32.GlobalLock
        global_lock.argtypes = (wintypes.HGLOBAL,)
        global_lock.restype = ctypes.c_void_p
        global_unlock = kernel32.GlobalUnlock
        global_unlock.argtypes = (wintypes.HGLOBAL,)
        global_unlock.restype = wintypes.BOOL

        image_data = []
        for clipboard_format, image_format in formats:
            if not clipboard_format or not is_available(clipboard_format):
                continue
            handle = get_data(clipboard_format)
            if not handle:
                continue
            size = global_size(handle)
            pointer = global_lock(handle)
            if not pointer:
                continue
            try:
                data = ctypes.string_at(pointer, size)
            finally:
                global_unlock(handle)

            if data:
                image_data.append((image_format, data))
        return True, image_data
    finally:
        user32.CloseClipboard()


def clipboard_image_data(image_format, data):
    if image_format == "dib":
        if len(data) < 40:
            raise ValueError("Clipboard DIB header is incomplete.")

        header_size, = struct.unpack_from("<I", data)
        if header_size < 40 or header_size > len(data):
            raise ValueError("Clipboard DIB header is invalid.")
        bits_per_pixel = struct.unpack_from("<H", data, 14)[0]
        compression = struct.unpack_from("<I", data, 16)[0]
        colors_used = struct.unpack_from("<I", data, 32)[0]
        pixel_offset = header_size
        if header_size == 40 and compression in (3, 6):
            pixel_offset += 16 if compression == 6 else 12
        if bits_per_pixel <= 8:
            pixel_offset += (colors_used or (1 << bits_per_pixel)) * 4
        bmp_header = b"BM" + struct.pack(
            "<IHHI",
            len(data) + 14,
            0,
            0,
            pixel_offset + 14,
        )
        return bmp_header + data
    return data


def same_image_content(first_format, first_data, second_format, second_data):
    try:
        with Image.open(
            BytesIO(clipboard_image_data(first_format, first_data))
        ) as first_image, Image.open(
            BytesIO(clipboard_image_data(second_format, second_data))
        ) as second_image:
            first_image.load()
            second_image.load()
            if first_image.size != second_image.size:
                return False
            return (
                first_image.convert("RGBA").tobytes()
                == second_image.convert("RGBA").tobytes()
            )
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        warnings.warn(f"Could not compare clipboard images: {error}", RuntimeWarning)
        return False


def open_clipboard_image(image_format, data, master):
    data = clipboard_image_data(image_format, data)

    with Image.open(BytesIO(data)) as image:
        image.load()
        image.thumbnail((92, 66), Image.Resampling.LANCZOS)
        return ImageTk.PhotoImage(image.convert("RGBA"), master=master)


def write_windows_clipboard_image(window, image_format, data):
    if sys.platform != "win32":
        raise OSError("Copying images to the clipboard is only supported on Windows.")

    data = clipboard_image_data(image_format, data)
    with Image.open(BytesIO(data)) as image:
        image.load()
        png_stream = BytesIO()
        image.convert("RGBA").save(png_stream, format="PNG")
        bmp_stream = BytesIO()
        image.convert("RGB").save(bmp_stream, format="BMP")
    png_data = png_stream.getvalue()
    dib_data = bmp_stream.getvalue()[14:]

    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    register_format = user32.RegisterClipboardFormatW
    register_format.argtypes = (wintypes.LPCWSTR,)
    register_format.restype = wintypes.UINT
    png_format = register_format("PNG")
    if not png_format:
        raise ctypes.WinError()

    open_clipboard = user32.OpenClipboard
    open_clipboard.argtypes = (wintypes.HWND,)
    open_clipboard.restype = wintypes.BOOL
    if not open_clipboard(window.winfo_id()):
        raise ctypes.WinError()

    allocated_handles = []
    try:
        empty_clipboard = user32.EmptyClipboard
        empty_clipboard.argtypes = ()
        empty_clipboard.restype = wintypes.BOOL
        if not empty_clipboard():
            raise ctypes.WinError()

        global_alloc = kernel32.GlobalAlloc
        global_alloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
        global_alloc.restype = wintypes.HGLOBAL
        global_lock = kernel32.GlobalLock
        global_lock.argtypes = (wintypes.HGLOBAL,)
        global_lock.restype = ctypes.c_void_p
        global_unlock = kernel32.GlobalUnlock
        global_unlock.argtypes = (wintypes.HGLOBAL,)
        global_unlock.restype = wintypes.BOOL
        global_free = kernel32.GlobalFree
        global_free.argtypes = (wintypes.HGLOBAL,)
        global_free.restype = wintypes.HGLOBAL
        set_clipboard_data = user32.SetClipboardData
        set_clipboard_data.argtypes = (wintypes.UINT, wintypes.HANDLE)
        set_clipboard_data.restype = wintypes.HANDLE

        for clipboard_format, clipboard_data in (
            (png_format, png_data),
            (8, dib_data),
        ):
            handle = global_alloc(0x0002, len(clipboard_data))
            if not handle:
                raise ctypes.WinError()
            allocated_handles.append(handle)
            pointer = global_lock(handle)
            if not pointer:
                raise ctypes.WinError()
            ctypes.memmove(pointer, clipboard_data, len(clipboard_data))
            global_unlock(handle)
            if not set_clipboard_data(clipboard_format, handle):
                raise ctypes.WinError()
            allocated_handles.remove(handle)
    finally:
        for handle in allocated_handles:
            global_free(handle)
        user32.CloseClipboard()


def draw_container(canvas, _event=None):
    canvas.delete("container")
    width = canvas.winfo_width()
    height = canvas.winfo_height()
    radius = 12

    if width <= radius * 2 or height <= radius * 2:
        return

    color = PANEL
    canvas.create_rectangle(
        radius,
        0,
        width - radius,
        height,
        fill=color,
        outline=color,
        tags="container",
    )
    canvas.create_rectangle(
        0,
        radius,
        width,
        height - radius,
        fill=color,
        outline=color,
        tags="container",
    )

    for x, y in (
        (0, 0),
        (width - radius * 2, 0),
        (0, height - radius * 2),
        (width - radius * 2, height - radius * 2),
    ):
        canvas.create_oval(
            x,
            y,
            x + radius * 2,
            y + radius * 2,
            fill=color,
            outline=color,
            tags="container",
        )


def draw_rounded_rectangle(canvas, x, y, width, height, radius, color, tags=None):
    canvas.create_rectangle(
        x + radius,
        y,
        x + width - radius,
        y + height,
        fill=color,
        outline=color,
        tags=tags,
    )
    canvas.create_rectangle(
        x,
        y + radius,
        x + width,
        y + height - radius,
        fill=color,
        outline=color,
        tags=tags,
    )
    for corner_x, corner_y in (
        (x, y),
        (x + width - radius * 2, y),
        (x, y + height - radius * 2),
        (x + width - radius * 2, y + height - radius * 2),
    ):
        canvas.create_oval(
            corner_x,
            corner_y,
            corner_x + radius * 2,
            corner_y + radius * 2,
            fill=color,
            outline=color,
            tags=tags,
        )


def fit_clipboard_text(text, font, width, max_lines):
    words = " ".join(text.split()).split()
    lines = []
    line = ""
    truncated = False

    for word in words:
        parts = []
        part = ""
        for character in word:
            if part and font.measure(part + character) > width:
                parts.append(part)
                part = character
            else:
                part += character
        if part:
            parts.append(part)

        for part in parts:
            candidate = f"{line} {part}".strip()
            if font.measure(candidate) <= width:
                line = candidate
            else:
                if line:
                    lines.append(line)
                line = part
                if len(lines) >= max_lines:
                    truncated = True
                    break
        if truncated:
            break

    if line and len(lines) < max_lines:
        lines.append(line)
    elif line:
        truncated = True

    if truncated and lines:
        last_line = lines[-1]
        ellipsis = "..."
        while last_line and font.measure(last_line + ellipsis) > width:
            last_line = last_line[:-1]
        lines[-1] = last_line + ellipsis
    return "\n".join(lines)


def enable_dark_title_bar(window):
    if sys.platform != "win32":
        return

    from ctypes import wintypes

    get_parent = ctypes.windll.user32.GetParent
    get_parent.argtypes = (wintypes.HWND,)
    get_parent.restype = wintypes.HWND
    hwnd = get_parent(window.winfo_id())
    dark_mode = wintypes.BOOL(True)
    set_attribute = ctypes.windll.dwmapi.DwmSetWindowAttribute
    set_attribute.argtypes = (
        wintypes.HWND,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    set_attribute.restype = ctypes.c_long

    for attribute in (20, 19):
        result = set_attribute(
            hwnd,
            attribute,
            ctypes.byref(dark_mode),
            ctypes.sizeof(dark_mode),
        )
        if result == 0:
            return

    warnings.warn("Windows could not enable the dark title bar.", RuntimeWarning)


def set_windows_app_icon(window, icon_path):
    if sys.platform != "win32":
        return ()

    from ctypes import wintypes

    user32 = ctypes.windll.user32
    load_image = user32.LoadImageW
    load_image.argtypes = (
        wintypes.HINSTANCE,
        wintypes.LPCWSTR,
        wintypes.UINT,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    )
    load_image.restype = wintypes.HANDLE
    load_from_file = 0x0010
    image_icon = 1
    icon_handles = []
    for width_metric, height_metric in ((11, 12), (49, 50)):
        width = user32.GetSystemMetrics(width_metric)
        height = user32.GetSystemMetrics(height_metric)
        icon_handle = load_image(
            None,
            str(icon_path),
            image_icon,
            width,
            height,
            load_from_file,
        )
        if not icon_handle:
            raise ctypes.WinError()
        icon_handles.append(icon_handle)

    get_ancestor = user32.GetAncestor
    get_ancestor.argtypes = (wintypes.HWND, wintypes.UINT)
    get_ancestor.restype = wintypes.HWND
    hwnd = get_ancestor(window.winfo_id(), 2)
    if not hwnd:
        raise ctypes.WinError()

    set_class_icon = (
        user32.SetClassLongPtrW
        if ctypes.sizeof(ctypes.c_void_p) == 8
        else user32.SetClassLongW
    )
    set_class_icon.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_void_p)
    set_class_icon.restype = ctypes.c_void_p
    set_class_icon(hwnd, -14, icon_handles[0])
    set_class_icon(hwnd, -34, icon_handles[1])

    send_message = user32.SendMessageW
    send_message.argtypes = (
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )
    send_message.restype = wintypes.LPARAM
    send_message(hwnd, 0x0080, 1, icon_handles[0])
    send_message(hwnd, 0x0080, 0, icon_handles[1])
    return tuple(icon_handles)


def create_footer_icon(kind, background, master, color):
    scale = 3
    size = 20 * scale
    center = size // 2
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    if kind == "sun":
        draw.ellipse(
            (center - 5 * scale, center - 5 * scale,
             center + 5 * scale, center + 5 * scale),
            outline=color,
            width=2 * scale,
        )
        for angle in range(0, 360, 45):
            radians = math.radians(angle)
            inner_radius = 7 * scale
            outer_radius = 9 * scale
            draw.line(
                (
                    center + math.cos(radians) * inner_radius,
                    center + math.sin(radians) * inner_radius,
                    center + math.cos(radians) * outer_radius,
                    center + math.sin(radians) * outer_radius,
                ),
                fill=color,
                width=2 * scale,
            )
    elif kind == "refresh":
        radius = 7 * scale
        for start_angle, end_angle in ((35, 155), (215, 335)):
            draw.arc(
                (
                    center - radius,
                    center - radius,
                    center + radius,
                    center + radius,
                ),
                start=start_angle,
                end=end_angle,
                fill=color,
                width=2 * scale,
            )
            angle = math.radians(end_angle)
            tip_x = center + math.cos(angle) * radius
            tip_y = center + math.sin(angle) * radius
            tangent_x = -math.sin(angle)
            tangent_y = math.cos(angle)
            normal_x = math.cos(angle)
            normal_y = math.sin(angle)
            draw.polygon(
                (
                    (tip_x, tip_y),
                    (
                        tip_x - tangent_x * 4 * scale - normal_x * 2 * scale,
                        tip_y - tangent_y * 4 * scale - normal_y * 2 * scale,
                    ),
                    (
                        tip_x - tangent_x * 4 * scale + normal_x * 2 * scale,
                        tip_y - tangent_y * 4 * scale + normal_y * 2 * scale,
                    ),
                ),
                fill=color,
            )
    else:
        raise ValueError(f"Unsupported footer icon: {kind!r}.")

    return ImageTk.PhotoImage(
        image.resize((16, 16), Image.Resampling.LANCZOS),
        master=master,
    )


def main():
    global BACKGROUND, PANEL, CARD

    app_root = Path(__file__).resolve().parent
    font_path = app_root / "Fonts" / "GoogleSans-VariableFont_GRAD,opsz,wght.ttf"
    app_font_registered = register_app_font(font_path)
    theme_name = load_theme()
    save_theme(theme_name)
    palette = THEMES[theme_name]
    BACKGROUND = palette["background"]
    PANEL = palette["panel"]
    CARD = palette["card"]
    text_color = palette["text"]
    muted_color = palette["muted"]
    icon_color = palette["icon"]

    window = tk.Tk()
    window.title("ClipStack")
    icon_path = app_root / "Icons" / "icon.ico"
    window.iconbitmap(default=str(icon_path))
    window.geometry("650x260")
    window.minsize(650, 260)
    window.maxsize(650, 260)
    window.configure(bg=BACKGROUND)
    window.withdraw()
    window.grid_columnconfigure(0, weight=1)
    window.grid_rowconfigure(0, weight=1)
    container = tk.Canvas(
        window,
        bg=BACKGROUND,
        highlightthickness=0,
        bd=0,
    )
    container.grid(row=0, column=0, sticky="nsew", padx=20, pady=(20, 6))
    container.bind("<Configure>", lambda event: draw_container(container, event))

    panel_content = tk.Frame(container, bg=PANEL)
    panel_window = container.create_window(12, 12, anchor="nw", window=panel_content)
    container.bind(
        "<Configure>",
        lambda event: (
            draw_container(container, event),
            container.itemconfigure(
                panel_window,
                width=max(1, event.width - 24),
                height=max(1, event.height - 24),
            ),
        ),
    )

    footer = tk.Canvas(
        window,
        bg=BACKGROUND,
        height=34,
        highlightthickness=0,
        bd=0,
    )
    footer.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 10))

    def draw_footer_background(event=None):
        footer.delete("footer_background")
        draw_rounded_rectangle(
            footer,
            0,
            0,
            event.width if event else footer.winfo_width(),
            event.height if event else footer.winfo_height(),
            12,
            PANEL,
            tags="footer_background",
        )
        footer.tag_lower("footer_background")

    footer.bind("<Configure>", draw_footer_background)
    tooltip_window = None
    tooltip_font = tkfont.Font(family=APP_FONT_FAMILY, size=9)
    dialog_font = tkfont.Font(family=APP_FONT_FAMILY, size=10)
    dialog_button_font = tkfont.Font(family=APP_FONT_FAMILY, size=9)

    def show_footer_tooltip(event, text):
        nonlocal tooltip_window
        if tooltip_window is None:
            transparent_color = "#010203" if sys.platform == "win32" else BACKGROUND
            tooltip_window = tk.Toplevel(window)
            tooltip_window.overrideredirect(True)
            tooltip_window.attributes("-topmost", True)
            tooltip_window.configure(bg=transparent_color)
            if sys.platform == "win32":
                tooltip_window.attributes("-transparentcolor", transparent_color)
            tooltip_canvas = tk.Canvas(
                tooltip_window,
                bg=transparent_color,
                highlightthickness=0,
                bd=0,
            )
            tooltip_canvas.pack()
            tooltip_window.tooltip_canvas = tooltip_canvas

        tooltip_canvas = tooltip_window.tooltip_canvas
        width = tooltip_font.measure(text) + 24
        height = 28
        tooltip_canvas.configure(width=width, height=height)
        tooltip_canvas.delete("all")
        draw_rounded_rectangle(
            tooltip_canvas,
            0,
            0,
            width,
            height,
            height // 2,
            CARD,
        )
        tooltip_canvas.create_text(
            width // 2,
            height // 2,
            text=text,
            fill=text_color,
            font=tooltip_font,
        )
        x = event.widget.winfo_rootx() + (event.widget.winfo_width() - width) // 2
        y = event.widget.winfo_rooty() - height - 6
        tooltip_window.geometry(f"{width}x{height}+{x}+{y}")
        tooltip_window.deiconify()

    def hide_footer_tooltip(_event=None):
        if tooltip_window is not None:
            tooltip_window.withdraw()

    footer_icon_images = [
        create_footer_icon("refresh", PANEL, window, icon_color),
        create_footer_icon("sun", PANEL, window, icon_color),
    ]
    footer_buttons = []
    for index, (icon_image, tooltip_text) in enumerate(
        zip(footer_icon_images, ("Restart Clipboard", "Change Theme"))
    ):
        button = tk.Canvas(
            footer,
            bg=PANEL,
            highlightthickness=0,
            bd=0,
            width=28,
            height=28,
            cursor="hand2",
        )
        draw_rounded_rectangle(button, 0, 0, 28, 28, 10, PANEL, tags="button_bg")
        button.icon_item = button.create_image(14, 14, image=icon_image)
        button.image = icon_image
        footer_buttons.append(button)
        button.bind(
            "<Leave>",
            lambda event, target=button: (
                target.itemconfigure("button_bg", fill=PANEL, outline=PANEL),
                hide_footer_tooltip(event),
            ),
        )
        button.bind(
            "<Enter>",
            lambda event, text=tooltip_text, target=button: (
                target.itemconfigure("button_bg", fill=CARD, outline=CARD),
                show_footer_tooltip(event, text),
            ),
        )
        button.place(x=4 + index * 34, y=3, width=28, height=28)

    card_font = tkfont.Font(family=APP_FONT_FAMILY, size=-11)
    timestamp_font = tkfont.Font(family=APP_FONT_FAMILY, size=-8)
    history_path = Path(__file__).resolve().with_name("clipboard_history.json")
    history = load_history(history_path, window)
    clear_history_dialog = None
    clear_history_dialog_image = None
    last_clipboard_sequence = None
    last_clipboard_text = None
    clipboard_contains_image = False
    hovered_index = None
    copied_index = None
    copied_feedback_timer = None
    card_lifts = {}
    animation_pending = False
    carousel = tk.Canvas(
        panel_content,
        bg=PANEL,
        highlightthickness=0,
        bd=0,
    )
    panel_content.grid_columnconfigure(0, weight=1)
    panel_content.grid_rowconfigure(0, weight=1)
    carousel.grid(row=0, column=0, sticky="nsew", padx=8, pady=(8, 8))
    scrollbar_height = 8
    carousel_scrollbar = tk.Canvas(
        panel_content,
        bg=PANEL,
        highlightthickness=0,
        bd=0,
        height=scrollbar_height,
        cursor="",
    )
    carousel_scrollbar.grid(
        row=1,
        column=0,
        sticky="ew",
        padx=8,
        pady=(4, 8),
    )
    carousel_scrollbar.grid_remove()

    scrollbar_thumb = {
        "start": 0.0,
        "end": 1.0,
        "drag_offset": 0.0,
        "pixel_start": 0.0,
        "pixel_width": 0.0,
    }

    def draw_carousel_scrollbar(first, last):
        scrollbar_thumb["start"] = first
        scrollbar_thumb["end"] = last
        carousel_scrollbar.delete("thumb")
        width = carousel_scrollbar.winfo_width()
        if width <= 1 or last - first >= 1:
            return
        thumb_width = max(24, width * (last - first))
        thumb_width = min(width, thumb_width)
        travel = width - thumb_width
        max_start = max(0.0, 1.0 - (last - first))
        thumb_start = (first / max_start * travel) if max_start else 0.0
        thumb_end = thumb_start + thumb_width
        scrollbar_thumb["pixel_start"] = thumb_start
        scrollbar_thumb["pixel_width"] = thumb_width
        draw_rounded_rectangle(
            carousel_scrollbar,
            thumb_start,
            2,
            thumb_end - thumb_start,
            4,
            2,
            palette["scrollbar"],
            tags="thumb",
        )

    def update_carousel_scrollbar(first, last):
        if len(history) > 4:
            if not carousel_scrollbar.winfo_manager():
                carousel_scrollbar.grid()
            draw_carousel_scrollbar(float(first), float(last))
        else:
            carousel_scrollbar.grid_remove()

    def scroll_to_pointer(event):
        width = carousel_scrollbar.winfo_width()
        thumb_width = scrollbar_thumb["pixel_width"]
        travel = width - thumb_width
        if travel <= 0:
            return
        thumb_start = scrollbar_thumb["pixel_start"]
        if thumb_start <= event.x <= thumb_start + thumb_width:
            scrollbar_thumb["drag_offset"] = event.x - thumb_start
        else:
            scrollbar_thumb["drag_offset"] = thumb_width / 2
        fraction = (
            (event.x - scrollbar_thumb["drag_offset"])
            / travel
            * max(0.0, 1.0 - (scrollbar_thumb["end"] - scrollbar_thumb["start"]))
        )
        carousel.xview_moveto(max(0.0, min(1.0, fraction)))

    def drag_scrollbar_thumb(event):
        width = carousel_scrollbar.winfo_width()
        thumb_width = scrollbar_thumb["pixel_width"]
        travel = width - thumb_width
        if travel <= 0:
            return
        fraction = (
            (event.x - scrollbar_thumb["drag_offset"])
            / travel
            * max(0.0, 1.0 - (scrollbar_thumb["end"] - scrollbar_thumb["start"]))
        )
        carousel.xview_moveto(max(0.0, min(1.0, fraction)))

    carousel_scrollbar.bind("<Button-1>", scroll_to_pointer)
    carousel_scrollbar.bind("<B1-Motion>", drag_scrollbar_thumb)
    carousel_scrollbar.bind(
        "<Enter>",
        lambda _event: carousel_scrollbar.itemconfigure(
            "thumb", fill=palette["scrollbar_hover"]
        ),
    )
    carousel_scrollbar.bind(
        "<Leave>",
        lambda _event: carousel_scrollbar.itemconfigure(
            "thumb", fill=palette["scrollbar"]
        ),
    )
    carousel_scrollbar.bind(
        "<Configure>",
        lambda _event: draw_carousel_scrollbar(
            scrollbar_thumb["start"], scrollbar_thumb["end"]
        ),
    )
    carousel.configure(xscrollcommand=update_carousel_scrollbar)

    def render_carousel(_event=None):
        carousel.delete("all")
        viewport_width = carousel.winfo_width()
        if not history:
            carousel.create_text(
                max(1, viewport_width) // 2,
                CARD_SIZE // 2,
                text="Copy text or images to start building your clipboard history.",
                fill=muted_color,
                font=card_font,
                anchor="center",
            )
            carousel.configure(scrollregion=(0, 0, max(1, viewport_width), CARD_SIZE))
            return

        card_top = max(
            HOVER_LIFT,
            (carousel.winfo_height() - CARD_SIZE) // 2,
        )
        for index, (
            item_type,
            content,
            copied_at,
            thumbnail,
            _image_format,
        ) in enumerate(history):
            x = 8 + index * (CARD_SIZE + CARD_GAP)
            lift = card_lifts.get(index, 0)
            y = card_top - lift
            draw_rounded_rectangle(
                carousel, x, y, CARD_SIZE, CARD_SIZE, 12, CARD
            )
            carousel.create_text(
                x + CARD_SIZE // 2,
                y + 14,
                text=copied_at.strftime("%Y-%m-%d %H:%M:%S"),
                fill=muted_color,
                font=timestamp_font,
                anchor="center",
            )
            if item_type == "image":
                carousel.create_image(
                    x + CARD_SIZE // 2,
                    y + 66,
                    image=thumbnail,
                    anchor="center",
                )
            else:
                display_text = fit_clipboard_text(
                    content,
                    card_font,
                    CARD_SIZE - 24,
                    MAX_TEXT_LINES,
                )
                carousel.create_text(
                    x + CARD_SIZE // 2,
                    y + 56,
                    text=display_text,
                    fill=text_color,
                    font=card_font,
                    width=CARD_SIZE - 24,
                    anchor="center",
                    justify="left",
                )
            if index == copied_index:
                carousel.create_line(
                    x + CARD_SIZE - 22,
                    y + CARD_SIZE - 15,
                    x + CARD_SIZE - 18,
                    y + CARD_SIZE - 11,
                    fill="#4caf50",
                    width=2,
                    capstyle="round",
                )
                carousel.create_line(
                    x + CARD_SIZE - 18,
                    y + CARD_SIZE - 11,
                    x + CARD_SIZE - 10,
                    y + CARD_SIZE - 21,
                    fill="#4caf50",
                    width=2,
                    capstyle="round",
                )

        content_width = 16 + len(history) * (CARD_SIZE + CARD_GAP) - CARD_GAP
        carousel.configure(
            scrollregion=(0, 0, max(viewport_width, content_width), CARD_SIZE)
        )

    def animate_hover():
        nonlocal animation_pending
        animation_pending = False
        still_animating = False
        for index in range(len(history)):
            lift = card_lifts.get(index, 0)
            target = HOVER_LIFT if index == hovered_index else 0
            if lift < target:
                card_lifts[index] = lift + 1
                still_animating = True
            elif lift > target:
                card_lifts[index] = lift - 1
                still_animating = True
        for index in tuple(card_lifts):
            if index >= len(history):
                del card_lifts[index]
        render_carousel()
        if still_animating:
            animation_pending = True
            window.after(16, animate_hover)

    def schedule_hover_animation():
        nonlocal animation_pending
        if not animation_pending:
            animation_pending = True
            window.after(16, animate_hover)

    def update_hover(event):
        nonlocal hovered_index
        x = carousel.canvasx(event.x)
        y = carousel.canvasy(event.y)
        relative_x = x - 8
        index = int(relative_x // (CARD_SIZE + CARD_GAP))
        card_top = max(
            HOVER_LIFT,
            (carousel.winfo_height() - CARD_SIZE) // 2,
        )
        within_card = (
            0 <= index < len(history)
            and relative_x % (CARD_SIZE + CARD_GAP) < CARD_SIZE
            and card_top - HOVER_LIFT <= y <= card_top + CARD_SIZE
        )
        new_index = index if within_card else None
        if new_index != hovered_index:
            hovered_index = new_index
            carousel.configure(cursor="hand2" if new_index is not None else "")
            schedule_hover_animation()

    def copy_carousel_item(event):
        nonlocal clipboard_contains_image, last_clipboard_sequence
        nonlocal copied_index, copied_feedback_timer
        x = carousel.canvasx(event.x)
        y = carousel.canvasy(event.y)
        relative_x = x - 8
        index = int(relative_x // (CARD_SIZE + CARD_GAP))
        if not (
            0 <= index < len(history)
            and relative_x % (CARD_SIZE + CARD_GAP) < CARD_SIZE
        ):
            return

        card_top = max(
            HOVER_LIFT,
            (carousel.winfo_height() - CARD_SIZE) // 2,
        )
        card_top -= card_lifts.get(index, 0)
        if not card_top <= y <= card_top + CARD_SIZE:
            return

        item_type, content, _copied_at, _thumbnail, image_format = history[index]
        try:
            if item_type == "text":
                window.clipboard_clear()
                window.clipboard_append(content)
                clipboard_contains_image = False
            else:
                write_windows_clipboard_image(window, image_format, content)
                clipboard_contains_image = True
            copied_index = 0
            record_history_item(
                (item_type, content, datetime.now(), _thumbnail, image_format)
            )
            if copied_feedback_timer is not None:
                window.after_cancel(copied_feedback_timer)
            copied_feedback_timer = window.after(3000, hide_copy_feedback)
            if sys.platform == "win32":
                get_clipboard_sequence = ctypes.windll.user32.GetClipboardSequenceNumber
                get_clipboard_sequence.argtypes = ()
                get_clipboard_sequence.restype = ctypes.c_uint
                last_clipboard_sequence = get_clipboard_sequence()
        except (
            OSError,
            ValueError,
            tk.TclError,
            Image.DecompressionBombError,
        ) as error:
            warnings.warn(
                f"Could not copy clipboard history item: {error}",
                RuntimeWarning,
            )

    def hide_copy_feedback():
        nonlocal copied_index, copied_feedback_timer
        copied_index = None
        copied_feedback_timer = None
        render_carousel()

    def clear_hover(_event=None):
        nonlocal hovered_index
        if hovered_index is not None:
            hovered_index = None
            carousel.configure(cursor="")
            schedule_hover_animation()

    def toggle_theme():
        nonlocal theme_name, palette, text_color, muted_color, icon_color
        global BACKGROUND, PANEL, CARD

        theme_name = "light" if theme_name == "dark" else "dark"
        palette = THEMES[theme_name]
        BACKGROUND = palette["background"]
        PANEL = palette["panel"]
        CARD = palette["card"]
        text_color = palette["text"]
        muted_color = palette["muted"]
        icon_color = palette["icon"]
        save_theme(theme_name)

        window.configure(bg=BACKGROUND)
        container.configure(bg=BACKGROUND)
        panel_content.configure(bg=PANEL)
        footer.configure(bg=BACKGROUND)
        draw_container(container)
        draw_footer_background()
        carousel.configure(bg=PANEL)
        carousel_scrollbar.configure(bg=PANEL)
        carousel_scrollbar.itemconfigure("thumb", fill=palette["scrollbar"])
        draw_carousel_scrollbar(
            scrollbar_thumb["start"],
            scrollbar_thumb["end"],
        )
        for button, kind in zip(footer_buttons, ("refresh", "sun")):
            button.configure(bg=PANEL)
            button.delete("all")
            draw_rounded_rectangle(
                button,
                0,
                0,
                28,
                28,
                10,
                PANEL,
                tags="button_bg",
            )
            icon_image = create_footer_icon(kind, PANEL, window, icon_color)
            button.icon_item = button.create_image(14, 14, image=icon_image)
            button.image = icon_image
        hide_footer_tooltip()
        render_carousel()

    footer_buttons[1].bind("<Button-1>", lambda _event: toggle_theme())

    carousel.bind("<Configure>", render_carousel)
    carousel.bind("<Motion>", update_hover)
    carousel.bind("<Button-1>", copy_carousel_item)
    carousel.bind("<Leave>", clear_hover)

    def record_history_item(entry):
        item_type, content, _copied_at, _thumbnail, image_format = entry
        for index in range(len(history) - 1, -1, -1):
            existing_type, existing_content, _, _, existing_format = history[index]
            matches = existing_type == item_type
            if matches and item_type == "text":
                matches = existing_content == content
            elif matches:
                matches = (
                    existing_format == image_format and existing_content == content
                ) or same_image_content(
                    existing_format,
                    existing_content,
                    image_format,
                    content,
                )
            if matches:
                del history[index]

        history.insert(0, entry)
        del history[MAX_HISTORY:]
        save_history(history_path, history)
        render_carousel()
        carousel.xview_moveto(0)

    def close_clear_history_dialog():
        nonlocal clear_history_dialog, clear_history_dialog_image
        if clear_history_dialog is not None:
            clear_history_dialog.grab_release()
            clear_history_dialog.destroy()
            clear_history_dialog = None
            clear_history_dialog_image = None
            window.lift()
            window.focus_force()

    def confirm_clear_history():
        nonlocal copied_index, copied_feedback_timer
        history.clear()
        card_lifts.clear()
        copied_index = None
        if copied_feedback_timer is not None:
            window.after_cancel(copied_feedback_timer)
            copied_feedback_timer = None
        save_history(history_path, history)
        render_carousel()
        carousel.xview_moveto(0)
        close_clear_history_dialog()

    def show_clear_history_dialog(_event=None):
        nonlocal clear_history_dialog, clear_history_dialog_image
        if clear_history_dialog is not None:
            return

        hide_footer_tooltip()
        window.update_idletasks()
        window_x = window.winfo_rootx()
        window_y = window.winfo_rooty()
        window_width = window.winfo_width()
        window_height = window.winfo_height()
        try:
            background_image = ImageGrab.grab(
                bbox=(
                    window_x,
                    window_y,
                    window_x + window_width,
                    window_y + window_height,
                )
            )
        except OSError as error:
            warnings.warn(
                f"Could not capture the app window for the confirmation dialog: {error}",
                RuntimeWarning,
            )
            background_image = Image.new(
                "RGB",
                (window_width, window_height),
                BACKGROUND,
            )
        if background_image.size != (window_width, window_height):
            background_image = background_image.resize(
                (window_width, window_height),
                Image.Resampling.LANCZOS,
            )
        clear_history_dialog_image = ImageTk.PhotoImage(
            ImageEnhance.Brightness(
                background_image.filter(ImageFilter.GaussianBlur(radius=8))
            ).enhance(0.72),
            master=window,
        )

        clear_history_dialog = tk.Canvas(
            window,
            bg=BACKGROUND,
            bd=0,
            highlightthickness=0,
            cursor="",
        )
        clear_history_dialog.place(x=0, y=0, relwidth=1, relheight=1)
        clear_history_dialog.create_image(
            0,
            0,
            image=clear_history_dialog_image,
            anchor="nw",
        )
        dialog_width = 420
        dialog_height = 166
        dialog_x = (window_width - dialog_width) // 2
        dialog_y = (window_height - dialog_height) // 2
        draw_rounded_rectangle(
            clear_history_dialog,
            dialog_x,
            dialog_y,
            dialog_width,
            dialog_height,
            12,
            PANEL,
        )
        clear_history_dialog.create_text(
            window_width // 2,
            dialog_y + 50,
            text="This action will clear your clipboard entirely.\nDo you wish to continue?",
            width=dialog_width - 48,
            justify="center",
            fill=text_color,
            font=dialog_font,
        )
        for x, label, tag, fill, hover_fill, foreground in (
            (
                window_width // 2 - 112,
                "Cancel",
                "cancel_button",
                CARD,
                palette["scrollbar"],
                text_color,
            ),
            (
                window_width // 2 + 12,
                "Confirm",
                "confirm_button",
                "#b84444",
                "#a33b3b",
                "#ffffff",
            ),
        ):
            button_y = dialog_y + 104
            draw_rounded_rectangle(
                clear_history_dialog,
                x,
                button_y,
                100,
                36,
                10,
                fill,
                tags=(tag, f"{tag}_background"),
            )
            clear_history_dialog.create_text(
                x + 50,
                button_y + 18,
                text=label,
                anchor="center",
                justify="center",
                fill=foreground,
                font=dialog_button_font,
                tags=tag,
            )

            def on_button_enter(_event, button_tag=tag, color=hover_fill):
                clear_history_dialog.itemconfigure(
                    f"{button_tag}_background",
                    fill=color,
                    outline=color,
                )
                clear_history_dialog.configure(cursor="hand2")

            def on_button_leave(_event, button_tag=tag, color=fill):
                clear_history_dialog.itemconfigure(
                    f"{button_tag}_background",
                    fill=color,
                    outline=color,
                )
                clear_history_dialog.configure(cursor="")

            clear_history_dialog.tag_bind(tag, "<Enter>", on_button_enter)
            clear_history_dialog.tag_bind(tag, "<Leave>", on_button_leave)
            clear_history_dialog.tag_bind(
                tag,
                "<Button-1>",
                (
                    (lambda _event: close_clear_history_dialog())
                    if tag == "cancel_button"
                    else (lambda _event: confirm_clear_history())
                ),
            )
        clear_history_dialog.bind(
            "<Escape>",
            lambda _event: close_clear_history_dialog(),
        )
        clear_history_dialog.grab_set()
        clear_history_dialog.focus_force()

    footer_buttons[0].bind("<Button-1>", show_clear_history_dialog)

    def capture_clipboard():
        nonlocal last_clipboard_sequence, last_clipboard_text
        nonlocal clipboard_contains_image

        clipboard_changed = False
        if sys.platform == "win32":
            get_clipboard_sequence = ctypes.windll.user32.GetClipboardSequenceNumber
            get_clipboard_sequence.argtypes = ()
            get_clipboard_sequence.restype = ctypes.c_uint
            sequence = get_clipboard_sequence()
            clipboard_changed = sequence != last_clipboard_sequence
            if clipboard_changed:
                clipboard_contains_image = False
                clipboard_opened, image_candidates = read_windows_clipboard_image(window)
                if clipboard_opened:
                    last_clipboard_sequence = sequence
                    clipboard_contains_image = bool(image_candidates)
                    thumbnail = None
                    image_bytes = None
                    selected_image_format = None
                    image_error = None
                    for image_format, candidate_data in image_candidates:
                        try:
                            thumbnail = open_clipboard_image(
                                image_format,
                                candidate_data,
                                window,
                            )
                        except (
                            OSError,
                            ValueError,
                            tk.TclError,
                            Image.DecompressionBombError,
                        ) as error:
                            image_error = error
                        else:
                            image_bytes = candidate_data
                            selected_image_format = image_format
                            break

                    if image_candidates and thumbnail is None:
                        warnings.warn(
                            f"Could not decode clipboard image: {image_error}",
                            RuntimeWarning,
                        )
                    elif thumbnail is not None:
                        record_history_item(
                            (
                                "image",
                                image_bytes,
                                datetime.now(),
                                thumbnail,
                                selected_image_format,
                            )
                        )

        if not clipboard_contains_image:
            try:
                text = window.clipboard_get()
            except tk.TclError:
                text = ""

            if sys.platform != "win32":
                clipboard_changed = text != last_clipboard_text
                last_clipboard_text = text
            if text and clipboard_changed:
                record_history_item(("text", text, datetime.now(), None, None))
        window.after(500, capture_clipboard)

    def show_window():
        window.deiconify()
        window.lift()
        window.focus_force()

    def hide_to_tray(_event=None):
        window.withdraw()

    def exit_application():
        if hotkey_thread_id[0] is not None:
            ctypes.windll.user32.PostThreadMessageW(
                hotkey_thread_id[0],
                0x0012,
                0,
                0,
            )
        tray_icon.stop()
        window.destroy()
        if app_font_registered:
            unregister_app_font(font_path)

    def schedule_show_window(_icon=None, _item=None):
        window.after(0, show_window)

    def schedule_exit_application(_icon=None, _item=None):
        window.after(0, exit_application)

    with Image.open(icon_path) as icon_source:
        icon_source.load()
        tray_image = icon_source.convert("RGBA")
        tray_image.thumbnail((64, 64), Image.Resampling.LANCZOS)
    tray_icon = pystray.Icon(
        "ClipStack",
        tray_image,
        "ClipStack",
        menu=pystray.Menu(
            pystray.MenuItem("Open ClipStack", schedule_show_window, default=True),
            pystray.MenuItem("Exit", schedule_exit_application),
        ),
    )
    tray_icon.run_detached()
    window.protocol("WM_DELETE_WINDOW", hide_to_tray)

    hotkey_thread_id = [None]

    def listen_for_hotkey():
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        get_current_thread_id = kernel32.GetCurrentThreadId
        get_current_thread_id.argtypes = ()
        get_current_thread_id.restype = wintypes.DWORD
        hotkey_thread_id[0] = get_current_thread_id()

        message = wintypes.MSG()
        get_message = user32.GetMessageW
        get_message.argtypes = (
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
        )
        get_message.restype = ctypes.c_int
        peek_message = user32.PeekMessageW
        peek_message.argtypes = (
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
        )
        peek_message.restype = wintypes.BOOL
        peek_message(ctypes.byref(message), None, 0, 0, 0)

        register_hotkey = user32.RegisterHotKey
        register_hotkey.argtypes = (
            wintypes.HWND,
            ctypes.c_int,
            wintypes.UINT,
            wintypes.UINT,
        )
        register_hotkey.restype = wintypes.BOOL
        hotkey_id = 1
        if not register_hotkey(None, hotkey_id, 0x0002 | 0x0001, ord("V")):
            warnings.warn(
                "Could not register the Ctrl+Alt+V global hotkey.",
                RuntimeWarning,
            )
            return

        try:
            while True:
                result = get_message(ctypes.byref(message), None, 0, 0)
                if result == -1:
                    raise ctypes.WinError()
                if result == 0:
                    break
                if message.message == 0x0312 and message.wParam == hotkey_id:
                    window.after(0, show_window)
        except tk.TclError:
            pass
        finally:
            user32.UnregisterHotKey(None, hotkey_id)

    window.update_idletasks()
    enable_dark_title_bar(window)
    _window_icon_handles = set_windows_app_icon(window, icon_path)
    window.after_idle(render_carousel)
    window.after_idle(
        lambda: threading.Thread(target=listen_for_hotkey, daemon=True).start()
    )
    if sys.platform == "win32":
        get_clipboard_sequence = ctypes.windll.user32.GetClipboardSequenceNumber
        get_clipboard_sequence.argtypes = ()
        get_clipboard_sequence.restype = ctypes.c_uint
        last_clipboard_sequence = get_clipboard_sequence()
    else:
        try:
            last_clipboard_text = window.clipboard_get()
        except tk.TclError:
            last_clipboard_text = ""
    window.after(0, capture_clipboard)
    show_window()
    window.mainloop()


if __name__ == "__main__":
    main()