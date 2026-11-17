"""Markdown -> speech sanitizer. Pure functions, unit-testable."""
import re

MAX_CHUNK = 2000

CODE_MARKER = "code omitted"

_fence_re = re.compile(r"```.*?```", re.DOTALL)
_inline_code_re = re.compile(r"`([^`]*)`")
_link_re = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
_image_re = re.compile(r"!\[([^\]]*)\]\(([^)]*)\)")
_url_re = re.compile(r"https?://\S+")
_heading_re = re.compile(r"^#{1,6}\s*", re.MULTILINE)
_md_chars_re = re.compile(r"[*_~]{1,3}")
_html_re = re.compile(r"<[^>]+>")
_ws_re = re.compile(r"[ \t]+")
_blank_re = re.compile(r"\n{3,}")


def sanitize(text: str) -> str:
    """Strip markdown for TTS. Returns plain speech text."""
    if not text:
        return ""
    t = text
    t = _image_re.sub(lambda m: m.group(1) or "image", t)
    t = _fence_re.sub(f" {CODE_MARKER}. ", t)
    t = _link_re.sub(r"\1", t)
    t = _url_re.sub("link", t)
    t = _inline_code_re.sub(r"\1", t)
    t = _heading_re.sub("", t)
    # bold/italic markers, strikethrough
    t = _md_chars_re.sub("", t)
    # blockquote / list / hr / heading leftovers
    lines = []
    for line in t.splitlines():
        s = line.strip()
        s = re.sub(r"^(>\s*)+", "", s)
        s = re.sub(r"^(\d+[.)]|[-*+])\s+", "", s)
        s = re.sub(r"^#{1,6}\s*", "", s)
        s = re.sub(r"^(-{3,}|_{3,}|\*{3,})$", "", s)
        # tables: drop separator rows, turn cells into a spoken list
        if re.fullmatch(r"\|?[\s:|-]*-[\s:|-]*\|?", s):
            s = ""
        elif s.startswith("|"):
            s = ", ".join(c.strip() for c in s.strip("|").split("|") if c.strip())
        # end each line as a sentence so TTS batches stay short
        if s and s[-1] not in ".!?:;,":
            s += "."
        lines.append(s)
    t = "\n".join(lines)
    t = _html_re.sub("", t)
    t = _ws_re.sub(" ", t)
    t = _blank_re.sub("\n\n", t)
    # collapse whitespace around newlines
    t = re.sub(r" *\n *", "\n", t)
    return t.strip()


def chunk(text: str, limit: int = MAX_CHUNK):
    """Split sanitized text into <= limit chunks on sentence/word boundaries."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    # split on sentence ends first
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks, cur = [], ""
    for s in sentences:
        if len(s) > limit:
            # hard split long sentence on words
            words = s.split(" ")
            for w in words:
                if len(cur) + len(w) + 1 > limit and cur:
                    chunks.append(cur.strip())
                    cur = ""
                cur = (cur + " " + w).strip()
            continue
        if len(cur) + len(s) + 1 > limit and cur:
            chunks.append(cur.strip())
            cur = s
        else:
            cur = (cur + " " + s).strip()
    if cur.strip():
        chunks.append(cur.strip())
    return [c for c in chunks if c]
