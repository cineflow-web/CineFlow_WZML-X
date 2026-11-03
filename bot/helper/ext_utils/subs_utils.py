from os import makedirs, path as ospath, walk
from re import compile as re_compile, sub as re_sub
from shutil import copyfile
from zipfile import ZipFile, BadZipFile

SUB_EXTS = (".srt", ".ass", ".vtt", ".sub")
_YEAR = re_compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_SITE = re_compile(r"^\s*(?:www\.)?[\w-]+(?:\.[\w-]+)+\s*-\s*")
_BAD = re_compile(r'[\\/:*?"<>|]')


_EP = re_compile(r"(?<![A-Za-z0-9])S(\d{1,2})[ ._-]?E(\d{1,3})(?:[-E]E?\d{1,3})?(?![0-9])", 2)
_EP_X = re_compile(r"(?<![A-Za-z0-9])(\d{1,2})x(\d{2,3})(?![0-9])", 2)


def parse_name(raw):
    """'Dorothy (2026).zip' -> 'Dorothy 2026 si'; series -> 'Show 2020 S01E02 si'.
    Returns the base name without extension, or None."""
    stem = ospath.splitext(ospath.basename(raw))[0]
    stem = _SITE.sub("", stem)
    ep = _EP.search(stem) or _EP_X.search(stem)
    epcode = f"S{int(ep.group(1)):02d}E{int(ep.group(2)):02d}" if ep else None
    m = _YEAR.search(stem)
    cut = min([x.start() for x in (m, ep) if x] or [0])
    if not m and not ep:
        return None
    title = stem[:cut]
    title = title.replace("&", " and ").replace("_", " ").replace(".", " ")
    title = re_sub(r"[\(\)\[\]]", " ", title)
    title = _BAD.sub(" ", title)
    title = re_sub(r"\s+", " ", title).strip(" -")
    if not title:
        return None
    if title.isupper() or title.islower():
        title = title.title()
    parts = [title]
    if m and (not ep or m.start() < ep.start()):
        parts.append(m.group(1))
    if epcode:
        parts.append(epcode)
    return " ".join(parts) + " si"


def _is_junk(name):
    return "__MACOSX" in name or ospath.basename(name).startswith("._")


def collect_subs(zip_path, work_dir, hint=None, depth=0):
    """Recursively extract zips; return list of (final_name, file_path)."""
    out = []
    if depth > 4:
        return out
    ext_dir = ospath.join(work_dir, f"x{depth}_{abs(hash((zip_path, hint)))}")
    makedirs(ext_dir, exist_ok=True)
    try:
        with ZipFile(zip_path) as z:
            members = [
                i for i in z.infolist() if not i.is_dir() and not _is_junk(i.filename)
            ]
            z.extractall(ext_dir, members=members)
    except BadZipFile:
        return out
    nsubs = sum(1 for i in members if i.filename.lower().endswith(SUB_EXTS))
    base_hint = parse_name(hint) if hint and nsubs == 1 else None
    for i in members:
        p = ospath.join(ext_dir, i.filename)
        low = i.filename.lower()
        if low.endswith(".zip"):
            out += collect_subs(p, work_dir, ospath.basename(i.filename), depth + 1)
        elif low.endswith(SUB_EXTS):
            parsed = base_hint or parse_name(i.filename)
            if not parsed:
                continue
            ext = ospath.splitext(low)[1]
            out.append((f"{parsed}{ext}", p))
    return out


def build_files(items, out_dir):
    """Copy subs to out_dir with final names (dedupe). Returns list of paths."""
    makedirs(out_dir, exist_ok=True)
    used, res = {}, []
    for name, p in items:
        if name in used:
            used[name] += 1
            stem, ext = ospath.splitext(name)
            name = f"{stem} ({used[name]}){ext}"
        else:
            used[name] = 1
        dst = ospath.join(out_dir, name)
        copyfile(p, dst)
        res.append(dst)
    return res


def process(paths, work_dir, out_dir):
    """paths: downloaded files (.zip or sub). Returns list of renamed sub paths."""
    items = []
    for p in paths:
        low = p.lower()
        if low.endswith(".zip"):
            items += collect_subs(p, work_dir, ospath.basename(p))
        elif low.endswith(SUB_EXTS):
            parsed = parse_name(p)
            if parsed:
                items.append(
                    (f"{parsed}{ospath.splitext(low)[1]}", p)
                )
    return build_files(items, out_dir) if items else []
