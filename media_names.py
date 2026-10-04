"""Uniform names for the downloaded and extracted media: IMG_<case>_<n> and VID_<case>_<n>.

<case> is the case number from the script's "Case N:" headings ("INTRO" for anything before the
first one, nothing at all for a script without headings), and <n> counts the distinct files in that
case in script order: IMG_5_1, IMG_5_2, ... then IMG_6_1 again. A file used twice keeps the name
from its first use, and every clip that uses it points at that one file.
"""
from pathlib import Path


def media_stem(prefix, case, number):
    """IMG_5_1, IMG_INTRO_1, or IMG_1 when the script has no cases (case is None)."""
    tag = "INTRO" if case == 0 else None if case is None else str(case)
    return "_".join(part for part in (prefix, tag, str(number)) if part)


def rename_media(entries, prefix):
    """Rename each distinct file to its uniform name and point every asset at the new path.

    entries: (asset, case) pairs in script order; an asset is a dict with a "path". Several
    entries may share a path (or even a dict); the first one decides the name. Returns
    {old path: new path}."""
    counts, plan = {}, {}
    for asset, case in entries:
        old = asset.get("path")
        if not old or old in plan:
            continue
        counts[case] = counts.get(case, 0) + 1
        source = Path(old)
        plan[old] = str(source.with_name(media_stem(prefix, case, counts[case]) + source.suffix.lower()))
    # Two steps, so no file is ever renamed onto a name another file still holds.
    staged = []
    for number, (old, new) in enumerate(plan.items()):
        if Path(old) != Path(new):
            holding = Path(old).with_name(f".renaming-{number}{Path(old).suffix}")
            Path(old).replace(holding)
            staged.append((holding, Path(new)))
    for holding, new in staged:
        holding.replace(new)
    for asset, _ in entries:
        if asset.get("path") in plan:
            asset["path"] = plan[asset["path"]]
    return plan
