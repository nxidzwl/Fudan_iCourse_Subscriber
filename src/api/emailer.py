import re
import smtplib
import time
import uuid
from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from io import BytesIO
from PIL import Image
from collections import OrderedDict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from html import escape
from email.utils import formataddr
from urllib.parse import quote

import markdown
from pygments.formatters import HtmlFormatter

from src.runtime import config

_MD_EXTENSIONS= ["tables", "fenced_code", "nl2br", "sane_lists", "codehilite"]

_MD_EXTENSION_CONFIGS = {
    "codehilite": {
        "guess_lang": False,
        "linenums": False,
        "css_class": "highlight",
    }
}

_PYGMENTS_CSS = HtmlFormatter(style="friendly").get_style_defs(".highlight")

_EMAIL_CSS = """\
body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 "Helvetica Neue", Arial, sans-serif;
    font-size: 15px;
    line-height: 1.7;
    color: #1a1a1a;
    max-width: 800px;
    margin: 0 auto;
    padding: 20px;
}
h2 {
    color: #2c3e50;
    border-bottom: 2px solid #3498db;
    padding-bottom: 8px;
    margin-top: 32px;
}
h3 {
    color: #34495e;
    margin-top: 24px;
}
h3 small {
    color: #7f8c8d;
    font-weight: normal;
}
h4 { color: #555; margin-top: 18px; }
hr {
    border: none;
    border-top: 1px solid #e0e0e0;
    margin: 28px 0;
}
strong { color: #c0392b; }
table {
    border-collapse: collapse;
    width: 100%;
    margin: 12px 0;
}
th, td {
    border: 1px solid #ddd;
    padding: 8px 12px;
    text-align: left;
}
th {
    background: #f5f6fa;
    font-weight: 600;
}
tr:nth-child(even) { background: #fafafa; }
pre {
    background: #f8f8f8;
    border: 1px solid #e0e0e0;
    border-radius: 4px;
    padding: 12px 16px;
    overflow-x: auto;
    font-size: 13px;
    line-height: 1.5;
}
code {
    font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
    font-size: 13px;
}
p code {
    background: #f0f0f0;
    padding: 2px 5px;
    border-radius: 3px;
}
blockquote {
    border-left: 4px solid #3498db;
    margin: 12px 0;
    padding: 8px 16px;
    background: #f8f9fa;
    color: #555;
}
ul, ol { padding-left: 24px; }
li { margin-bottom: 4px; }
"""

_MIN_INLINE_HEIGHT = 13  # minimum logical height for inline formulas (px)

_IMAGE_CACHE: dict[str, tuple] = {}
_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_CJK_TEXT_RE = re.compile(
    r"\\(?:text|mbox|mathrm|operatorname)\{([^{}]*[\u3400-\u9fff\uf900-\ufaff][^{}]*)\}"
    r"|[（(【\[]?\s*[\u3400-\u9fff\uf900-\ufaff]"
    r"[\u3400-\u9fff\uf900-\ufaff，。、；：！？、\sA-Za-z0-9%％°℃/·.\-_]*"
    r"\s*[）)】\]]?"
)


def _fetch_latex_image(url: str, dpi: int = 300) -> tuple:
    """Fetch rendered LaTeX image, return (width, height, png_bytes).

    Width/height are logical display pixels (DPI-adjusted).
    Returns (None, None, None) on failure.
    """
    if url in _IMAGE_CACHE:
        return _IMAGE_CACHE[url]

    try:
        scale_factor = dpi / 96.0
        response = requests.get(url, timeout=10)
        response.raise_for_status()

        img = Image.open(BytesIO(response.content))
        logical_width = max(1, int(img.width / scale_factor))
        logical_height = max(1, int(img.height / scale_factor))

        result = (logical_width, logical_height, response.content)
        _IMAGE_CACHE[url] = result
        return result
    except Exception as e:
        print(f"[LaTeX Render] Image fetch failed: {e}")
        return None, None, None


def _prefetch_latex_images(urls: list[str], dpi: int = 300) -> None:
    """Pre-fetch multiple LaTeX images concurrently.

    Results are stored in ``_IMAGE_CACHE`` so that subsequent calls to
    ``_fetch_latex_image`` become instant cache hits.
    """
    uncached = [u for u in urls if u not in _IMAGE_CACHE]
    if not uncached:
        return
    with ThreadPoolExecutor(max_workers=min(len(uncached), 8)) as pool:
        futures = {pool.submit(_fetch_latex_image, u, dpi): u for u in uncached}
        for future in as_completed(futures):
            future.result()  # trigger any exception logging inside _fetch_latex_image


def _split_latex_cjk(latex_content: str) -> list[tuple[str, str]]:
    """Split CJK text out of LaTeX content so CodeCogs need not render it."""
    if not _CJK_RE.search(latex_content):
        return [("math", latex_content)]

    parts: list[tuple[str, str]] = []
    pos = 0
    for match in _CJK_TEXT_RE.finditer(latex_content):
        if match.start() > pos:
            math_part = latex_content[pos:match.start()]
            if math_part.strip():
                parts.append(("math", math_part))
        text_part = match.group(1) if match.group(1) is not None else match.group(0)
        if text_part.strip():
            parts.append(("text", text_part.strip()))
        pos = match.end()

    if pos < len(latex_content):
        math_part = latex_content[pos:]
        if math_part.strip():
            parts.append(("math", math_part))
    return parts or [("text", latex_content)]


def _latex_url(latex_content: str, is_block: bool) -> str:
    prefix = r"\dpi{300}\bg{white}" if is_block else r"\dpi{300}\bg{white}\inline"
    return f"https://latex.codecogs.com/png.latex?{prefix}%20{quote(latex_content)}"


def _latex_image_html(url: str, latex_content: str, is_block: bool,
                      cid_images: dict | None, embed_images: bool) -> str:
    w, h, img_data = _fetch_latex_image(url)

    if not (w and h):
        return f'<code>{escape(latex_content)}</code>'

    if not is_block and h < _MIN_INLINE_HEIGHT:
        scale = _MIN_INLINE_HEIGHT / h
        w = max(1, int(w * scale))
        h = _MIN_INLINE_HEIGHT

    src = _resolve_src(url, img_data, cid_images, embed_images)
    if is_block:
        style = (
            f"width:{w}px;height:{h}px;max-width:none;"
            "vertical-align:middle;border:none;display:inline-block;"
        )
    else:
        style = (
            f"width:{w}px;height:{h}px;max-width:none;"
            "vertical-align:-3px;border:none;margin:0 2px;"
        )
    return (
        f'<img src="{src}" alt="{escape(latex_content)}" '
        f'width="{w}" height="{h}" style="{style}">'
    )


def _latex_text_html(text: str, is_block: bool) -> str:
    style = "vertical-align:middle;margin:0 2px;" if is_block else ""
    return f'<span style="{style}">{escape(text)}</span>'


def _md_to_html(md_text: str, cid_images: dict | None = None,
                embed_images: bool = False) -> str:
    """Convert Markdown to styled HTML, rendering LaTeX math as images.

    Processing order: extract LaTeX → markdown convert → restore as <img>.
    This prevents the markdown engine from corrupting backslash escapes.

    Args:
        md_text: Markdown source text.
        cid_images: When provided (dict), download images and embed via CID
                    references instead of external URLs.  The dict is populated
                    with {cid_name: png_bytes} entries for the caller to attach
                    to the MIME message.
        embed_images: When True, embed rendered LaTeX PNGs as data URIs.  This
                      is used for PDF generation so WeasyPrint does not need to
                      fetch remote formula images while rendering the document.
    """
    latex_map: dict[str, str] = {}
    counter = 0

    def _stash_latex(value: str) -> str:
        nonlocal counter
        key = f"\x00LATEX{counter}\x00"
        counter += 1
        latex_map[key] = value
        return key

    def _stash(match):
        return _stash_latex(match.group(0))

    def _stash_dollar_block(match):
        return match.group(1) + _stash_latex("$$" + match.group(2) + "$$")

    def _stash_dollar_inline(match):
        return match.group(1) + _stash_latex("$" + match.group(2) + "$")

    def _stash_block(match):
        """Stash \\[...\\] as $$...$$ for uniform downstream handling."""
        return _stash_latex("$$" + match.group(1) + "$$")

    def _stash_inline(match):
        """Stash \\(...\\) as $...$ for uniform downstream handling."""
        return _stash_latex("$" + match.group(1) + "$")

    # Extract LaTeX in order: block first, then inline
    # 1) $$...$$ block formulas
    text = re.sub(r"(^|[^\\])\$\$(.+?)\$\$", _stash_dollar_block,
                  md_text, flags=re.DOTALL)
    # 2) \[...\] block formulas (normalize to $$...$$)
    text = re.sub(r"\\\[(.+?)\\\]", _stash_block, text, flags=re.DOTALL)
    # 3) $...$ inline formulas (not $$)
    text = re.sub(r"(^|[^\\$])\$(?!\$)((?:\\.|[^\n\\$])+?)\$(?!\$)",
                  _stash_dollar_inline, text)
    # 4) \(...\) inline formulas (normalize to $...$)
    text = re.sub(r"\\\((.+?)\\\)", _stash_inline, text)

    html = markdown.markdown(
        text,
        extensions=_MD_EXTENSIONS,
        extension_configs=_MD_EXTENSION_CONFIGS,
    )

    # Build URL list and pre-fetch all LaTeX images concurrently.
    # CJK text is split out and rendered as normal HTML text because the
    # remote LaTeX image service does not reliably support Chinese glyphs.
    latex_info: dict[str, tuple[list[tuple[str, str]], bool]] = {}
    image_urls: list[str] = []
    for key, original in latex_map.items():
        is_block = original.startswith("$$")
        latex_content = original[2:-2] if is_block else original[1:-1]
        parts = _split_latex_cjk(latex_content)
        latex_info[key] = (parts, is_block)
        image_urls.extend(
            _latex_url(value, is_block)
            for kind, value in parts
            if kind == "math"
        )

    _prefetch_latex_images(image_urls)

    for key, (parts, is_block) in latex_info.items():
        rendered_parts = []
        for kind, value in parts:
            if kind == "text":
                rendered_parts.append(_latex_text_html(value, is_block))
            else:
                url = _latex_url(value, is_block)
                rendered_parts.append(
                    _latex_image_html(
                        url, value, is_block, cid_images, embed_images
                    )
                )

        if is_block:
            img_tag = (
                f'<div style="text-align:center;margin:16px 0">'
                f'{"".join(rendered_parts)}</div>'
            )
        else:
            img_tag = "".join(rendered_parts)

        html = html.replace(key, img_tag)

    return html


def _resolve_src(url: str, img_data: bytes | None,
                 cid_images: dict | None, embed_images: bool = False) -> str:
    """Return a CID reference if embedding, otherwise the original URL."""
    if embed_images and img_data:
        encoded = b64encode(img_data).decode("ascii")
        return f"data:image/png;base64,{encoded}"
    if cid_images is not None and img_data:
        cid = f"latex-{uuid.uuid4().hex[:12]}"
        cid_images[cid] = img_data
        return f"cid:{cid}"
    return url


class Emailer:
    """Send course summary emails via QQ SMTP SSL."""

    def __init__(self):
        self.host = config.SMTP_HOST
        self.port = config.SMTP_PORT
        self.sender = config.SMTP_EMAIL
        self.password = config.SMTP_PASSWORD
        self.receiver = config.RECEIVER_EMAIL

    def send(self, items: list[dict]) -> bool:
        """Send a single email containing all lecture summaries.

        LaTeX formulas are rendered as PNG images and embedded directly into
        the email via CID attachments, so they display on all clients
        (including mobile) without loading external images.

        Args:
            items: List of dicts, each with keys:
                   course_title, sub_title, date, summary
                   Optional key:
                   is_update — bool, True for re-summarized lectures (v2
                               PPT-aware format replacing an older v1 summary).
                               Adds an "（含 PPT 识别·更新）" subject suffix and
                               an inline 更新 badge per affected lecture.

        Returns:
            True if email was sent successfully, False otherwise.
        """
        if not items:
            return True

        any_update = any(item.get("is_update") for item in items)

        # Group by course (preserve insertion order)
        courses: OrderedDict[str, list[dict]] = OrderedDict()
        for item in items:
            courses.setdefault(item["course_title"], []).append(item)

        # Subject
        parts = [f"{ct} ({len(lecs)})" for ct, lecs in courses.items()]
        subject = f"[FiCS] {', '.join(parts)}"
        if any_update:
            subject += "（含 PPT 识别·更新）"

        # Plain text (Markdown as-is, readable without rendering)
        plain_sections = []
        for course_title, lectures in courses.items():
            plain_sections.append(f"{'=' * 40}")
            plain_sections.append(f"课程：{course_title}")
            plain_sections.append(f"{'=' * 40}")
            for lec in lectures:
                tag = "[更新] " if lec.get("is_update") else ""
                plain_sections.append(
                    f"\n--- {tag}{lec['sub_title']} ({lec['date']}) ---\n"
                )
                plain_sections.append(lec["summary"])
        plain = "\n".join(plain_sections)

        # HTML (Markdown → styled HTML with CID-embedded LaTeX images)
        cid_images: dict[str, bytes] = {}

        # Pre-compute one anchor ID per course so TOC and headings stay in sync
        course_anchors = {
            course_title: f"course-{i}"
            for i, course_title in enumerate(courses)
        }

        # Build table of contents
        toc_items = [
            f'<li><a href="#{anchor}" style="color:#3498db;text-decoration:none;">'
            f"{escape(course_title)}</a></li>"
            for course_title, anchor in course_anchors.items()
        ]
        toc_html = (
            '<nav style="background:#f8f9fa;border:1px solid #e0e0e0;'
            'border-radius:6px;padding:16px 20px;margin-bottom:28px;">'
            '<strong style="color:#2c3e50;font-size:16px;">目录</strong>'
            '<ol style="margin:8px 0 0;padding-left:20px;">'
            + "\n".join(toc_items)
            + "</ol></nav>"
        )

        update_badge = (
            '<span style="background:#ff9800;color:white;padding:2px 8px;'
            'border-radius:3px;font-size:12px;margin-right:8px;'
            'vertical-align:middle;">更新</span>'
        )

        body_parts = [toc_html]
        for course_title, lectures in courses.items():
            anchor = course_anchors[course_title]
            body_parts.append(f'<h2 id="{anchor}">{escape(course_title)}</h2>')
            for lec in lectures:
                badge = update_badge if lec.get("is_update") else ""
                body_parts.append(
                    f"<h3>{badge}{escape(lec['sub_title'])} "
                    f"<small>({escape(lec['date'])})</small></h3>"
                )
                body_parts.append(
                    _md_to_html(lec["summary"], cid_images=cid_images)
                )
                body_parts.append("<hr>")

        html = (
            "<!DOCTYPE html>"
            "<html><head><meta charset='utf-8'>"
            f"<style>{_EMAIL_CSS}\n{_PYGMENTS_CSS}</style>"
            "</head><body>"
            + "\n".join(body_parts)
            + "</body></html>"
        )

        # Build MIME: related > alternative > (plain, html) + image attachments
        msg = MIMEMultipart("related")
        msg["Subject"] = subject
        msg["From"] = formataddr(("iCourse Subscriber", self.sender))
        msg["To"] = self.receiver

        msg_alt = MIMEMultipart("alternative")
        msg_alt.attach(MIMEText(plain, "plain", "utf-8"))
        msg_alt.attach(MIMEText(html, "html", "utf-8"))
        msg.attach(msg_alt)

        # Attach CID images
        for cid, png_data in cid_images.items():
            img_part = MIMEImage(png_data, "png")
            img_part.add_header("Content-ID", f"<{cid}>")
            img_part.add_header("Content-Disposition", "inline",
                                filename=f"{cid}.png")
            msg.attach(img_part)

        if cid_images:
            print(f"[Emailer] Embedded {len(cid_images)} LaTeX images as CID")

        # Retry with exponential backoff
        for attempt in range(3):
            try:
                with smtplib.SMTP_SSL(self.host, self.port) as server:
                    server.login(self.sender, self.password)
                    server.sendmail(self.sender, self.receiver, msg.as_string())
                print(f"[Emailer] Sent: {subject}")
                return True
            except Exception as e:
                print(f"[Emailer] Attempt {attempt + 1}/3 failed: {e}")
                if attempt < 2:
                    time.sleep(2 ** attempt)

        print("[Emailer] All send attempts failed.")
        return False
