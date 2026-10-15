#!/usr/bin/env python3
"""Convert an approved .docx legal document to Markdown, numbering intact.

Word's clause numbers are not in the text: they are auto-numbering, computed at
render time from numbering.xml. Every text or Markdown exporter drops them, which
turns "register in accordance with clause 2" into a reference to a bullet. This
resolves the counters the way Word does and writes the numbers out literally, so
the Markdown reads the same as the document and the cross-references still land.

    python3 docx-to-md.py input.docx > TRIAL_TERMS.md

Check the result against the .docx before publishing. The .docx is authoritative.
"""
import html
import re
import sys
import zipfile

HEADING = {"Heading": 2, "CCPGHeading": 2, "Heading1": 2,
           "CCPGSubheading": 3, "CCPGSubheading2": 3, "CCPGSubheading3": 4}


def levels(numbering, abstract_id):
    """{ilvl: (numFmt, lvlText, start)} for one abstract numbering definition."""
    blk = re.search(r'<w:abstractNum [^>]*w:abstractNumId="%s".*?</w:abstractNum>'
                    % abstract_id, numbering, re.S)
    out = {}
    if not blk:
        return out
    for lvl in re.findall(r"<w:lvl [^>]*w:ilvl=\"\d+\".*?</w:lvl>", blk.group(0), re.S):
        i = int(re.search(r'w:ilvl="(\d+)"', lvl).group(1))
        fmt = re.search(r'<w:numFmt w:val="([^"]+)"', lvl)
        txt = re.search(r'<w:lvlText w:val="([^"]*)"', lvl)
        start = re.search(r'<w:start w:val="(\d+)"', lvl)
        out[i] = (fmt.group(1) if fmt else "decimal",
                  txt.group(1) if txt else "%1.",
                  int(start.group(1)) if start else 1)
    return out


def as_letter(n):
    """1 -> a, 26 -> z, 27 -> aa, the way Word counts lowerLetter."""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(ord("a") + r) + s
    return s


ROMAN = [(1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
         (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")]


def as_roman(n):
    out = ""
    for value, sign in ROMAN:
        while n >= value:
            out += sign
            n -= value
    return out


def render(fmt, n):
    if fmt == "lowerLetter":
        return as_letter(n)
    if fmt == "upperLetter":
        return as_letter(n).upper()
    if fmt == "lowerRoman":
        return as_roman(n)
    if fmt == "upperRoman":
        return as_roman(n).upper()
    return str(n)


def runs(p):
    """Paragraph text, with bold kept because the documents bold defined terms."""
    out = []
    for r in re.findall(r"<w:r[ >].*?</w:r>", p, re.S):
        text = "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", r, re.S))
        if not text:
            continue
        text = html.unescape(re.sub(r"<[^>]+>", "", text))
        # Escape the three Markdown marks that appear mid-sentence in legal prose.
        text = text.replace("\\", "\\\\").replace("*", r"\*").replace("_", r"\_")
        # <w:b w:val="0"/> means bold OFF. Matching the bare '<w:b ' prefix
        # published the limitation-of-liability clause bold, where the author had
        # switched off the bold its heading style imposes.
        bold = bool(re.search(r"<w:b/>|<w:b [^>]*/>", r)) and not re.search(
            r'<w:b [^>]*w:val="(?:0|false)"', r)
        # The ** markers have to sit inside the run's own whitespace: Markdown
        # ignores "** bold **", and stripping the space instead welds the run to
        # its neighbour ("**Email:**address").
        if bold and text.strip():
            lead = text[:len(text) - len(text.lstrip())]
            tail = text[len(text.rstrip()):]
            out.append("%s**%s**%s" % (lead, text.strip(), tail))
        else:
            out.append(text)
    return "".join(out).strip()


def convert(path):
    z = zipfile.ZipFile(path)
    doc = z.read("word/document.xml").decode("utf-8")
    numbering = z.read("word/numbering.xml").decode("utf-8")

    num_to_abstract = {}
    for nid, body in re.findall(r'<w:num [^>]*w:numId="(\d+)"[^>]*>(.*?)</w:num>',
                                numbering, re.S):
        a = re.search(r'<w:abstractNumId w:val="(\d+)"', body)
        if a:
            num_to_abstract[nid] = a.group(1)

    defs, counters, out = {}, {}, []

    for p in re.findall(r"<w:p[ >].*?</w:p>", doc, re.S):
        style = re.search(r'<w:pStyle w:val="([^"]+)"', p)
        style = style.group(1) if style else ""
        text = runs(p)
        if not text:
            continue

        nid = re.search(r'<w:numId w:val="(\d+)"', p)
        ilvl = re.search(r'<w:ilvl w:val="(\d+)"', p)

        if nid and nid.group(1) in num_to_abstract:
            abstract = num_to_abstract[nid.group(1)]
            level = int(ilvl.group(1)) if ilvl else 0
            if abstract not in defs:
                defs[abstract] = levels(numbering, abstract)
                counters[abstract] = {}
            fmt, lvl_text, start = defs[abstract].get(level, ("decimal", "%1.", 1))

            c = counters[abstract]
            c[level] = c.get(level, start - 1) + 1
            # A level incrementing restarts everything beneath it, as Word does.
            for deeper in [k for k in c if k > level]:
                del c[deeper]

            label = lvl_text
            for i in range(level + 1):
                f = defs[abstract].get(i, ("decimal", "", 1))[0]
                label = label.replace("%%%d" % (i + 1), render(f, c.get(i, 1)))

            # Top-level clauses are headings; everything under them is an indented
            # paragraph carrying its own number, which is what the document shows.
            if level == 0 and fmt == "decimal":
                out.append("\n### %s. %s\n\n" % (label, text))
            else:
                # Blank line, not one newline. GitHub renders repo markdown with
                # line breaks OFF, so single-spaced clauses collapse into one
                # paragraph and "3.1 ... 3.2 ..." arrives as a wall of text.
                out.append("%s%s %s\n\n" % ("    " * max(level - 1, 0), label, text))
            continue

        depth = HEADING.get(style)
        out.append("\n%s %s\n\n" % ("#" * depth, text) if depth else "%s\n\n" % text)

    return "".join(out)


if __name__ == "__main__":
    sys.stdout.write(convert(sys.argv[1]))
