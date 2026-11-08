#!/usr/bin/env python3
"""OCR XML の冊子ページを、ゲームIDごとの manual にマークアップするための補助処理。

Gemini API は generate サブコマンドが呼ぶ。座標は送らず、規則は system 指示に固定する。
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request

import certifi
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

import lxml.etree as LET

NAME_RE = re.compile(
    r"^(RCGSa\d{7})_([A-Za-z]\d{3})_(\d{3})\s*(?:_converted)?\.xml$"
)

SECTION_TYPES = {
    "character",
    "control",
    "gameplay",
    "item",
    "system",
    "narrative",
    "instruction",
    "reference",
    "warning",
    "legal",
    "cover",
    "preface",
    "greeting",
    "toc",
    "company_info",
    "blank",
    "memo",
    "map",
    "music",
    "credit",
}
PART_TYPES = {"button", "stick", "dpad", "touchScreen"}
INLINE = {
    "character",
    "gameLocation",
    "gameTime",
    "gameItem",
    "device",
    "partOfDevice",
    "person",
    "org",
    "location",
    "time",
    "copyRight",
    "credit",
}
ENTITY_TYPE_ATTR = {
    "character",
    "gameLocation",
    "gameTime",
    "gameItem",
    "device",
    "person",
    "org",
    "location",
    "time",
    "role",
}
SECTION_CHILDREN = {"head", "p", "list", "credit", "copyRight", "section"}
CREDIT_CHILDREN = {"role", "person", "org", "location", "time"}
NO_DIRECT_TEXT = {"page", "section", "list"}
PAGE_OPEN_RE = re.compile(r"<page\b", re.I)
PAGE_CLOSE_RE = re.compile(r"</page>", re.I)


def escape_attr(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace("\r", "")
        .replace("\n", " ")
    )


def is_indent(text: str) -> bool:
    return re.fullmatch(r"[ \t\r\n]*", text or "") is not None


def norm_text(text: str) -> str:
    return re.sub(r"[\t\r\n]", "", text)


def element_text(el: ET.Element) -> str:
    parts: list[str] = []
    for node in el.iter():
        for chunk in (node.text, node.tail):
            if not chunk or is_indent(chunk):
                continue
            parts.append(norm_text(chunk))
    return "".join(parts)


def read_page_xml(path: Path) -> tuple[str, list[tuple[str, str]]]:
    """本文行だけを読む。SHAPE、POLYGON、図版の座標は送らない。"""
    root = ET.parse(path).getroot()
    image = ""
    lines: list[tuple[str, str]] = []
    for el in root.iter():
        if el.tag in {"SHAPE", "POLYGON", "BLOCK"}:
            continue
        if el.tag == "PAGE" and not image:
            image = el.attrib.get("IMAGENAME", "")
        if el.tag != "LINE":
            continue
        text = el.attrib.get("STRING", "")
        if text == "":
            continue
        lines.append((el.attrib.get("TYPE", ""), text))
    if not image:
        image = path.stem + ".webp"
    return image, lines


def choose_duplicate(paths: list[Path]) -> Path:
    converted = [p for p in paths if "_converted" in p.name]
    if len(converted) == 1:
        return converted[0]
    if len(converted) > 1:
        names = ", ".join(p.name for p in converted)
        raise SystemExit(f"同じページの _converted XML が複数あります: {names}")
    if len(paths) == 1:
        return paths[0]
    names = ", ".join(p.name for p in paths)
    raise SystemExit(f"同じページの XML を一つに決められません: {names}")


def collect_pages(input_dir: Path) -> list[dict]:
    grouped: dict[tuple[str, str, int], list[Path]] = defaultdict(list)
    for path in input_dir.iterdir():
        if not path.is_file() or not path.name.endswith(".xml"):
            continue
        match = NAME_RE.match(path.name)
        if not match:
            raise SystemExit(f"ファイル名を解釈できません: {path.name}")
        game, booklet, page_no = match.group(1), match.group(2), int(match.group(3))
        grouped[(game, booklet, page_no)].append(path)

    chosen: list[tuple[str, str, int, Path]] = []
    for key, paths in grouped.items():
        chosen.append((key[0], key[1], key[2], choose_duplicate(paths)))
    chosen.sort(key=lambda item: (item[0], item[1], item[2]))

    rows: list[dict] = []
    seq_of: dict[str, int] = defaultdict(int)
    for game, booklet, page_no, path in chosen:
        seq_of[game] += 1
        image, lines = read_page_xml(path)
        rows.append(
            {
                "game": game,
                "seq": seq_of[game],
                "booklet": booklet,
                "file_page": page_no,
                "nlines": len(lines),
                "xml_path": str(path.resolve()),
                "image": image,
            }
        )
    return rows


def write_manifest(input_dir: Path, out_path: Path) -> list[dict]:
    rows = collect_pages(input_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write("# game\tseq\tbooklet\tfile_page\tnlines\txml_path\timage\n")
        for row in rows:
            fh.write(
                "\t".join(
                    [
                        row["game"],
                        str(row["seq"]),
                        row["booklet"],
                        str(row["file_page"]),
                        str(row["nlines"]),
                        row["xml_path"],
                        row["image"],
                    ]
                )
                + "\n"
            )
    return rows


def read_manifest(path: Path) -> dict[str, list[dict]]:
    games: dict[str, list[dict]] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            game, seq, booklet, file_page, nlines, xml_path, image = line.split("\t", 6)
            games.setdefault(game, []).append(
                {
                    "game": game,
                    "seq": int(seq),
                    "booklet": booklet,
                    "file_page": int(file_page),
                    "nlines": int(nlines),
                    "xml_path": xml_path,
                    "image": image,
                }
            )
    return games


def existing_page_numbers(path: Path) -> list[int]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    return [int(n) for n in re.findall(r'<page\b[^>]*\bnumber="(\d+)"', text)]


def ensure_closed(path: Path) -> None:
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    if re.search(r"</manual>\s*$", text):
        return
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text + "</manual>\n", encoding="utf-8")


def game_id_of(value: str) -> str:
    match = re.match(r"(RCGSa\d{7})", value or "")
    return match.group(1) if match else value


def reset_job(job_dir: Path) -> None:
    if job_dir.name != ".job":
        raise SystemExit(f"ジョブディレクトリを初期化できません: {job_dir}")
    if job_dir.exists():
        for child in job_dir.iterdir():
            if child.is_file():
                child.unlink()
            elif child.is_dir():
                raise SystemExit(f"ジョブディレクトリに想定外のディレクトリがあります: {child}")
    job_dir.mkdir(parents=True, exist_ok=True)


def load_lines(xml_path: str) -> list[tuple[str, str]]:
    _image, lines = read_page_xml(Path(xml_path))
    return lines


def render_input(batch: list[dict]) -> str:
    blocks: list[str] = []
    for row in batch:
        lines = load_lines(row["xml_path"])
        header = [
            f'number="{row["seq"]}"',
            f'img="{escape_attr(row["image"])}"',
            f'source="{escape_attr(Path(row["xml_path"]).name)}"',
            "---",
        ]
        if not lines:
            header.append("（テキスト行はありません。section type=\"blank\" だけを出力してください）")
        else:
            header.extend(f"{kind}\t{text}" for kind, text in lines)
        blocks.append("\n".join(header))
    return "\n\n".join(blocks) + "\n"


def write_status(job_dir: Path, values: dict[str, str]) -> None:
    lines = [f"{key}={values[key]}" for key in values]
    (job_dir / "status").write_text("\n".join(lines) + "\n", encoding="utf-8")


def cmd_manifest(args: argparse.Namespace) -> int:
    rows = write_manifest(Path(args.input), Path(args.out))
    games = len({row["game"] for row in rows})
    print(f"manifest games={games} pages={len(rows)} -> {args.out}")
    return 0


def select_games(games: dict[str, list[dict]], only: str, start_from: str) -> list[str]:
    names = sorted(games)
    if only:
        only_id = game_id_of(only)
        if only_id not in games:
            raise SystemExit(f"指定されたゲームが入力にありません: {only}")
        return [only_id]
    if start_from:
        start_id = game_id_of(start_from)
        return [name for name in names if name >= start_id]
    return names


def cmd_summary(args: argparse.Namespace) -> int:
    games = read_manifest(Path(args.manifest))
    output_dir = Path(args.output_dir)
    names = select_games(games, args.only or "", args.from_game or "")
    total = 0
    done = 0
    pending_games = 0
    first = ""
    for name in names:
        rows = games[name]
        total += len(rows)
        present = set(existing_page_numbers(output_dir / f"{name}.xml"))
        have = sum(1 for row in rows if row["seq"] in present)
        done += have
        if have < len(rows):
            pending_games += 1
            if not first:
                nxt = next(row for row in rows if row["seq"] not in present)
                first = f"{name} seq={nxt['seq']} {Path(nxt['xml_path']).name}"
    pending = total - done
    print(f"games={len(names)} pages={total} done={done} pending={pending} pending_games={pending_games}")
    if first:
        print(f"next={first}")
    else:
        print("next=none")
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    games = read_manifest(Path(args.manifest))
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    job_dir = Path(args.job_dir)
    names = select_games(games, args.only or "", args.from_game or "")
    batch_size = max(1, int(args.pages_per_call))

    for name in names:
        rows = games[name]
        out_path = output_dir / f"{name}.xml"
        seqs = [row["seq"] for row in rows]
        present = existing_page_numbers(out_path)
        present_set = set(present)
        unknown = present_set.difference(seqs)
        if unknown:
            raise SystemExit(f"{out_path} に想定外の page number があります: {sorted(unknown)}")
        missing = [seq for seq in seqs if seq not in present_set]
        if present_set and missing and min(missing) < max(present_set):
            raise SystemExit(
                f"{out_path} のページ番号に抜けがあります。"
                f" ある番号={sorted(present_set)} 無い番号={missing}"
            )
        if not missing:
            ensure_closed(out_path)
            continue

        by_seq = {row["seq"]: row for row in rows}
        first = by_seq[missing[0]]
        if first["nlines"] == 0:
            batch = [first]
            blank = True
        else:
            batch = []
            for seq in missing:
                row = by_seq[seq]
                if row["nlines"] == 0:
                    break
                batch.append(row)
                if len(batch) >= batch_size:
                    break
            blank = False

        reset_job(job_dir)
        (job_dir / "input.txt").write_text(render_input(batch), encoding="utf-8")
        labels = [Path(row["xml_path"]).name for row in batch]
        write_status(
            job_dir,
            {
                "game": name,
                "need_title": "0" if out_path.exists() else "1",
                "blank": "1" if blank else "0",
                "n_pages": str(len(batch)),
                "seqs": ",".join(str(row["seq"]) for row in batch),
                "is_last": "1" if len(batch) == len(missing) else "0",
                "total_pages": str(len(rows)),
                "done_before": str(len(present_set)),
                "label": ",".join(labels),
            },
        )
        print(
            f"JOB {name} seq={batch[0]['seq']}"
            + (f"-{batch[-1]['seq']}" if len(batch) > 1 else "")
            + f" {labels[0]}"
        )
        return 0

    print("DONE")
    return 2


def strip_ansi(text: str) -> str:
    text = text.replace("\ufeff", "")
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)


def extract_complete_pages(text: str) -> tuple[list[str], bool]:
    text = strip_ansi(text)
    fences = re.findall(r"```(?:xml)?\s*(.*?)```", text, flags=re.S | re.I)
    blob = "\n".join(fences) if fences else text
    pages: list[str] = []
    pos = 0
    truncated = False
    while True:
        opened = PAGE_OPEN_RE.search(blob, pos)
        if not opened:
            break
        start = opened.start()
        closed = PAGE_CLOSE_RE.search(blob, start)
        if not closed:
            truncated = True
            break
        pages.append(blob[start : closed.end()])
        pos = closed.end()
    return pages, truncated


def force_page_open(page_xml: str, number: int, image: str) -> str:
    opening = f'<page number="{number}" img="{escape_attr(image)}">'
    updated, count = re.subn(r"<page\b[^>]*>", opening, page_xml, count=1, flags=re.S)
    if count != 1:
        raise ValueError("page 開始タグを置き換えられません")
    return updated


def check_attrs(el: ET.Element) -> str | None:
    attrs = set(el.attrib)
    if el.tag == "page":
        allowed = {"number", "img"}
    elif el.tag == "section":
        allowed = {"type", "id"}
        if "id" in el.attrib:
            del el.attrib["id"]
            attrs.discard("id")
        section_type = el.attrib.get("type", "")
        if section_type and section_type not in SECTION_TYPES:
            return f"section type が一覧にありません: {section_type}"
    elif el.tag == "partOfDevice":
        allowed = {"type"}
        part_type = el.attrib.get("type", "")
        if part_type not in PART_TYPES:
            return f"partOfDevice の type は {' / '.join(sorted(PART_TYPES))} のいずれかです: {part_type or '（なし）'}"
    elif el.tag in ENTITY_TYPE_ATTR:
        allowed = {"type"}
    else:
        allowed = set()
    unknown = attrs.difference(allowed)
    if unknown:
        return f"{el.tag} に使えない属性があります: {', '.join(sorted(unknown))}"
    return None


def check_tree(el: ET.Element, parent: str | None) -> str | None:
    if el.tag == "page":
        allowed_children = {"section"}
    elif el.tag == "section":
        allowed_children = SECTION_CHILDREN
    elif el.tag == "list":
        allowed_children = {"li"}
    elif el.tag == "credit":
        allowed_children = CREDIT_CHILDREN
    elif el.tag in {"head", "p", "li", "copyRight"}:
        allowed_children = INLINE
    elif el.tag in INLINE or el.tag == "role":
        allowed_children = set()
    else:
        return f"使えない要素です: {el.tag}"

    if parent == "inline" and el.tag not in INLINE and el.tag != "role":
        return f"文中に {el.tag} は置けません"
    message = check_attrs(el)
    if message:
        return message
    if el.tag in NO_DIRECT_TEXT:
        if el.text and not is_indent(el.text):
            return f"{el.tag} の直下にテキストがあります。p や li や head に入れてください"
        for child in list(el):
            if child.tail and not is_indent(child.tail):
                return f"{el.tag} の直下にテキストがあります。p や li や head に入れてください"
    if el.tag == "page" and not list(el):
        return "page には section が必要です"
    for child in list(el):
        if not isinstance(child.tag, str):
            return "XMLコメント以外の想定外のノードがあります"
        if child.tag not in allowed_children:
            return f"{el.tag} の中に {child.tag} は置けません"
        child_parent = "inline" if el.tag in {"head", "p", "li", "copyRight"} else el.tag
        message = check_tree(child, child_parent)
        if message:
            return message
    return None


def stable_system_instruction(prompt_text: str, schema_text: str) -> str:
    """毎回同じバイト列にする。ページ本文は含めない。"""
    return (
        prompt_text.rstrip()
        + "\n\n## 検証に使うXSD\n"
        + schema_text.strip()
        + "\n"
    )


def thinking_config(model: str) -> dict:
    """2.x は thinkingBudget 0。gemini-3.8-flash など 3.x は low。Lite だけ minimal。"""
    if model.startswith("gemini-3"):
        if "lite" in model:
            return {"thinkingLevel": "minimal"}
        return {"thinkingLevel": "low"}
    return {"thinkingBudget": 0}


def build_generate_body(system: str, user: str, temperature: float, model: str) -> dict:
    return {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "temperature": temperature,
            "thinkingConfig": thinking_config(model),
        },
    }


_SCHEMA: dict[str, LET.XMLSchema] = {}


def load_xsd(schema_path: Path) -> LET.XMLSchema:
    key = str(schema_path.resolve())
    cached = _SCHEMA.get(key)
    if cached is None:
        cached = LET.XMLSchema(LET.parse(key))
        _SCHEMA[key] = cached
    return cached


def xsd_error(document_xml: str, schema_path: Path) -> str | None:
    try:
        document = LET.fromstring(document_xml.encode("utf-8"))
    except LET.XMLSyntaxError as exc:
        return f"XSD検証の前にXMLとして読めません: {exc}"
    schema = load_xsd(schema_path)
    if schema.validate(document):
        return None
    lines = [str(entry) for entry in schema.error_log]
    return "XSD検証エラー:\n" + "\n".join(lines[:12])


def xsd_error_for_pages(page_xmls: list[str], schema_path: Path) -> str | None:
    wrapped = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<manual title="markup" lang="ja" identifier="RCGSa0000000" model="markup">\n'
        + "\n".join(page.strip() for page in page_xmls)
        + "\n</manual>\n"
    )
    return xsd_error(wrapped, schema_path)


def response_text_from_api(payload: dict) -> str:
    candidates = payload.get("candidates") or []
    if not candidates:
        feedback = payload.get("promptFeedback") or {}
        reason = feedback.get("blockReason") or payload
        raise RuntimeError(f"API応答に本文がありません: {reason}")
    parts = candidates[0].get("content", {}).get("parts") or []
    texts = [part.get("text", "") for part in parts if part.get("text") and not part.get("thought")]
    return "".join(texts)


def usage_line(payload: dict) -> str:
    usage = payload.get("usageMetadata") or {}
    return (
        "USAGE "
        f"prompt={usage.get('promptTokenCount', 0)} "
        f"cached={usage.get('cachedContentTokenCount', 0)} "
        f"output={usage.get('candidatesTokenCount', 0)} "
        f"thoughts={usage.get('thoughtsTokenCount', 0)}"
    )


def api_error_message(body: str, status: int) -> str:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return f"API_ERROR {status} {body[:500]}"
    error = payload.get("error", {})
    message = error.get("message", body[:500])
    code = error.get("code", status)
    return f"API_ERROR {code} {error.get('status', '')} {message}".strip()


def cmd_generate(args: argparse.Namespace) -> int:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print("API_ERROR GEMINI_API_KEY がありません", file=sys.stderr)
        return 1
    system = stable_system_instruction(
        Path(args.system).read_text(encoding="utf-8"),
        Path(args.schema).read_text(encoding="utf-8"),
    )
    user = Path(args.user).read_text(encoding="utf-8")
    body = build_generate_body(system, user, float(args.temperature), args.model)
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{args.model}:generateContent"
    )
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )
    err_path = Path(args.err)
    out_path = Path(args.out)
    context = ssl.create_default_context(cafile=certifi.where())
    try:
        with urllib.request.urlopen(request, timeout=int(args.timeout), context=context) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        err_path.write_text(api_error_message(detail, exc.code) + "\n", encoding="utf-8")
        out_path.write_text("", encoding="utf-8")
        return 1
    except urllib.error.URLError as exc:
        text = str(exc)
        if "CERTIFICATE_VERIFY_FAILED" in text or "certificate verify failed" in text:
            err_path.write_text(f"SSL_ERROR {text}\n", encoding="utf-8")
            code = 1
        elif "timed out" in text:
            err_path.write_text(f"TIMEOUT after {args.timeout}s\n{text}\n", encoding="utf-8")
            code = 124
        else:
            err_path.write_text(f"API_ERROR {text}\n", encoding="utf-8")
            code = 1
        out_path.write_text("", encoding="utf-8")
        return code
    payload = json.loads(raw)
    err_path.write_text(usage_line(payload) + "\n", encoding="utf-8")
    out_path.write_text(response_text_from_api(payload), encoding="utf-8")
    return 0


KNOWN_TAGS = {"page", "section", "head", "p", "list", "li", "role"} | INLINE | SECTION_CHILDREN | CREDIT_CHILDREN


def unwrap_unknown(parent: ET.Element) -> None:
    """スキーマに無い要素は外し、中の文字と子要素は残す。"""
    for child in list(parent):
        unwrap_unknown(child)
    for child in list(parent):
        if not isinstance(child.tag, str) or child.tag in KNOWN_TAGS:
            continue
        index = list(parent).index(child)
        if child.text and not is_indent(child.text):
            if index == 0:
                parent.text = (parent.text or "") + child.text
            else:
                previous = list(parent)[index - 1]
                previous.tail = (previous.tail or "") + child.text
        insert_at = index
        for grand in list(child):
            child.remove(grand)
            parent.insert(insert_at, grand)
            insert_at += 1
        tail = child.tail
        parent.remove(child)
        if tail and not is_indent(tail):
            if insert_at == 0:
                parent.text = (parent.text or "") + tail
            else:
                list(parent)[insert_at - 1].tail = (list(parent)[insert_at - 1].tail or "") + tail


def lift_later_heads(section: ET.Element) -> list[ET.Element]:
    """section の先頭以外にある head を、同じ type の後続 section に分ける。文字は動かさない。"""
    extras: list[ET.Element] = []
    current = section
    while True:
        children = list(current)
        split_at = next(
            (i for i, child in enumerate(children) if child.tag == "head" and i != 0),
            None,
        )
        if split_at is None:
            break
        new_section = ET.Element("section", dict(current.attrib))
        for child in children[split_at:]:
            current.remove(child)
            new_section.append(child)
        extras.append(new_section)
        current = new_section
    return extras


def normalize_heads(parent: ET.Element) -> None:
    index = 0
    while index < len(list(parent)):
        child = list(parent)[index]
        if isinstance(child.tag, str) and child.tag == "section":
            normalize_heads(child)
            extras = lift_later_heads(child)
            for offset, extra in enumerate(extras, start=1):
                parent.insert(index + offset, extra)
            index += len(extras)
        index += 1


def escape_bare_lt(page_xml: str) -> str:
    """タグでない < （例: 。</</p>）を文字として残す。"""
    return re.sub(r"<(?!/?[A-Za-z]|!--|\?)", "&lt;", page_xml)


def validate_page(page_xml: str) -> tuple[ET.Element | None, str | None]:
    try:
        el = ET.fromstring(escape_bare_lt(page_xml))
    except ET.ParseError as exc:
        return None, f"XMLとして読めません: {exc}"
    if el.tag != "page":
        return None, "ルートが page ではありません"
    unwrap_unknown(el)
    normalize_heads(el)
    message = check_tree(el, None)
    if message:
        return None, message
    return el, None


def diff_message(expected: str, got: str) -> str:
    limit = min(len(expected), len(got))
    index = 0
    while index < limit and expected[index] == got[index]:
        index += 1

    def window(value: str) -> str:
        start = max(0, index - 16)
        end = min(len(value), index + 16)
        return value[start:end]

    return (
        f"地のテキストが一致しません。expected={len(expected)}文字 got={len(got)}文字 差分位置={index}\n"
        f"expected: {window(expected)}\n"
        f"got     : {window(got)}"
    )


def read_status(job_dir: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (job_dir / "status").read_text(encoding="utf-8").splitlines():
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def batch_from_job(job_dir: Path) -> list[dict]:
    status = read_status(job_dir)
    input_text = (job_dir / "input.txt").read_text(encoding="utf-8")
    chunks = [chunk for chunk in re.split(r"\n\s*\n", input_text.strip()) if chunk.strip()]
    seqs = [int(part) for part in status["seqs"].split(",") if part]
    if len(chunks) != len(seqs):
        raise SystemExit(f"入力ページ数と seq が一致しません: chunks={len(chunks)} seqs={seqs}")
    batch = []
    for seq, chunk in zip(seqs, chunks):
        number = re.search(r'number="(\d+)"', chunk)
        image = re.search(r'img="([^"]*)"', chunk)
        source = re.search(r'source="([^"]+)"', chunk)
        if not (number and image and source):
            raise SystemExit("ジョブ入力のヘッダを読めません")
        # テキストは元XMLから取り直す。プロンプト用の整形と突き合わせ対象を分ける。
        batch.append({"seq": seq, "image": html.unescape(image.group(1)), "source": source.group(1)})
    return batch


def source_text_for(job_dir: Path, seq: int) -> str:
    """input.txt ではなく、ジョブ作成時に保存した原文を使う。"""
    path = job_dir / f"source_{seq}.txt"
    return path.read_text(encoding="utf-8")


def store_sources(job_dir: Path, batch: list[dict]) -> None:
    for row in batch:
        lines = load_lines(row["xml_path"])
        text = "".join(text for _kind, text in lines)
        (job_dir / f"source_{row['seq']}.txt").write_text(text, encoding="utf-8")


def cmd_next_with_sources(args: argparse.Namespace) -> int:
    code = cmd_next(args)
    if code != 0:
        return code
    job_dir = Path(args.job_dir)
    status = read_status(job_dir)
    # cmd_next は原文ファイルをまだ書いていない。input.txt の元になった XML を
    # status と manifest から引き直す。
    games = read_manifest(Path(args.manifest))
    rows = {row["seq"]: row for row in games[status["game"]]}
    seqs = [int(part) for part in status["seqs"].split(",") if part]
    store_sources(job_dir, [rows[seq] for seq in seqs])
    return 0


def parse_title(text: str, game: str, source: str) -> str:
    match = re.search(r"(?m)^[ \t>*-]*TITLE:[ \t]*(.+?)\s*$", strip_ansi(text))
    if not match:
        return game
    title = match.group(1).strip().strip("`").strip()
    if title == game:
        return game
    folded_title = re.sub(r"\s+", "", title)
    folded_source = re.sub(r"\s+", "", source)
    if folded_title and folded_title in folded_source:
        return title
    return game


def manual_open(title: str, game: str, model: str) -> str:
    return (
        f'<manual title="{escape_attr(title)}" lang="ja" '
        f'identifier="{escape_attr(game)}" model="{escape_attr(model)}">'
    )


def resolve_model(args: argparse.Namespace) -> str:
    return getattr(args, "model", "") or os.environ.get("GEMINI_MODEL", "") or "markup"


def commit_pages(out_path: Path, game: str, title: str, model: str, pages: list[str], close: bool) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        body = out_path.read_text(encoding="utf-8")
        body = re.sub(r"\s*</manual>\s*$", "\n", body)
        kept = re.search(r'<manual\b[^>]*\btitle="([^"]*)"', body)
        if kept:
            title = html.unescape(kept.group(1))
        body = re.sub(r"<manual\b[^>]*>", manual_open(title, game, model), body, count=1)
    else:
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            + manual_open(title, game, model)
            + "\n"
        )
    if body and not body.endswith("\n"):
        body += "\n"
    for page in pages:
        body += page.strip() + "\n"
    if close:
        body += "</manual>\n"
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, out_path)


def fail(job_dir: Path, message: str) -> int:
    (job_dir / "last_error.txt").write_text(message[:4000], encoding="utf-8")
    print(message)
    return 1


def response_text(response_path: Path, job_dir: Path, continued: bool) -> str:
    fresh = response_path.read_text(encoding="utf-8", errors="replace")
    if not continued:
        return fresh
    partial = job_dir / "partial_raw.txt"
    previous = partial.read_text(encoding="utf-8", errors="replace") if partial.exists() else ""
    return previous + "\n" + fresh


def cmd_accept(args: argparse.Namespace) -> int:
    job_dir = Path(args.job_dir)
    output_dir = Path(args.output_dir)
    status = read_status(job_dir)
    game = status["game"]
    seqs = [int(part) for part in status["seqs"].split(",") if part]
    combined = response_text(Path(args.response), job_dir, args.continue_output)
    if len(combined) > 500_000:
        return fail(job_dir, "応答が長すぎるため、このページを最初からやり直してください")

    pages, truncated = extract_complete_pages(combined)
    if len(pages) > len(seqs):
        return fail(job_dir, f"page 要素が多すぎます。期待={len(seqs)} 実際={len(pages)}")
    if len(pages) < len(seqs):
        if truncated:
            (job_dir / "partial_raw.txt").write_text(combined, encoding="utf-8")
            print("TRUNCATED")
            return 4
        return fail(job_dir, f"page 要素が足りません。期待={len(seqs)} 実際={len(pages)}")

    batch = batch_from_job(job_dir)
    rendered: list[str] = []
    mismatches: list[str] = []
    for row, page_xml in zip(batch, pages):
        try:
            rewritten = force_page_open(page_xml, row["seq"], row["image"])
        except ValueError as exc:
            return fail(job_dir, str(exc))
        el, message = validate_page(rewritten)
        if message or el is None:
            return fail(job_dir, message or "page を検証できません")
        expected = norm_text(source_text_for(job_dir, row["seq"]))
        got = element_text(el)
        if expected != got:
            mismatches.append(f"seq={row['seq']} " + diff_message(expected, got))
        rendered.append(ET.tostring(el, encoding="unicode"))

    schema_path = Path(args.schema) if getattr(args, "schema", "") else default_schema()
    message = xsd_error_for_pages(rendered, schema_path)
    if message:
        return fail(job_dir, message)

    source_all = "".join(source_text_for(job_dir, seq) for seq in seqs)
    title = game
    if status.get("need_title") == "1":
        title = parse_title(combined, game, source_all)
    out_path = output_dir / f"{game}.xml"
    commit_pages(out_path, game, title, resolve_model(args), rendered, status.get("is_last") == "1")
    partial = job_dir / "partial_raw.txt"
    if partial.exists():
        partial.unlink()
    error_file = job_dir / "last_error.txt"
    if error_file.exists():
        error_file.unlink()
    if mismatches:
        print("地の文の差分は記録しました。ページは保存して次へ進みます。")
        print("\n".join(mismatches))
    print(f"OK {game} seq={status['seqs']}")
    return 0


def cmd_accept_blank(args: argparse.Namespace) -> int:
    job_dir = Path(args.job_dir)
    status = read_status(job_dir)
    if status.get("blank") != "1":
        return fail(job_dir, "このジョブは空ページではありません")
    batch = batch_from_job(job_dir)
    if len(batch) != 1 or source_text_for(job_dir, batch[0]["seq"]) != "":
        return fail(job_dir, "空ページとして記録できません")
    row = batch[0]
    page = (
        f'<page number="{row["seq"]}" img="{escape_attr(row["image"])}">\n'
        '  <section type="blank"/>\n'
        "</page>"
    )
    el, message = validate_page(page)
    if message or el is None:
        return fail(job_dir, message or "空ページを検証できません")
    schema_path = Path(args.schema) if getattr(args, "schema", "") else default_schema()
    message = xsd_error_for_pages([page], schema_path)
    if message:
        return fail(job_dir, message)
    game = status["game"]
    title = game if status.get("need_title") == "1" else game
    commit_pages(
        Path(args.output_dir) / f"{game}.xml",
        game,
        title,
        resolve_model(args),
        [page],
        status.get("is_last") == "1",
    )
    print(f"OK {game} seq={status['seqs']} blank")
    return 0


def default_schema() -> Path:
    return Path(__file__).resolve().parent / "game_manual_schema.xsd"


def write_sample(path: Path, image: str, lines: list[tuple[str, str]]) -> None:
    body = [f'<PAGE IMAGENAME="{image}">']
    for kind, text in lines:
        body.append(f'<LINE TYPE="{kind}" STRING="{escape_attr(text)}" />')
    body.append("</PAGE>")
    path.write_text("<OCRDATASET>" + "".join(body) + "</OCRDATASET>", encoding="utf-8")


def self_test() -> int:
    import shutil
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="markup-manual-"))
    try:
        src = root / "in"
        src.mkdir()
        write_sample(
            src / "RCGSa0000001_m001_002_converted.xml",
            "m001-2.webp",
            [("タイトル本文", "もくじ"), ("本文", "◆操作")],
        )
        write_sample(
            src / "RCGSa0000001_m001_001_converted.xml",
            "m001-1.webp",
            [("本文", "このたびは、誠に"), ("本文", "ありがとうございました。")],
        )
        write_sample(
            src / "RCGSa0000001_m001_001.xml",
            "ignored.webp",
            [("本文", "使わない方")],
        )
        write_sample(src / "RCGSa0000001_p001_001_converted.xml", "poster.webp", [])
        write_sample(
            src / "RCGSa0000001_m002_001_converted.xml",
            "m002.webp",
            [("本文", "第二冊")],
        )
        manifest = root / "manifest.tsv"
        rows = write_manifest(src, manifest)
        assert [row["seq"] for row in rows] == [1, 2, 3, 4], rows
        assert [row["booklet"] for row in rows] == ["m001", "m001", "m002", "p001"]
        assert Path(rows[0]["xml_path"]).name.endswith("_converted.xml")
        assert rows[0]["image"] == "m001-1.webp"
        assert rows[2]["nlines"] == 1
        assert rows[3]["nlines"] == 0

        out = root / "out"
        job = root / ".job"
        ns = argparse.Namespace(
            manifest=str(manifest),
            output_dir=str(out),
            job_dir=str(job),
            only="",
            from_game="",
            pages_per_call=1,
        )
        assert cmd_next_with_sources(ns) == 0
        status = read_status(job)
        assert status["seqs"] == "1" and status["need_title"] == "1" and status["blank"] == "0"

        good = (
            "TITLE: このたびは、誠に\n"
            '<page number="9" img="wrong.webp">'
            "<section type=\"preface\">"
            "<p>このたびは、誠にありがとうございました。</p>"
            "</section></page>\n"
        )
        response = job / "response.txt"
        response.write_text(good, encoding="utf-8")
        accept_ns = argparse.Namespace(
            job_dir=str(job),
            output_dir=str(out),
            response=str(response),
            continue_output=False,
        )
        assert cmd_accept(accept_ns) == 0, (job / "last_error.txt").read_text() if (job / "last_error.txt").exists() else ""
        saved = (out / "RCGSa0000001.xml").read_text(encoding="utf-8")
        assert 'number="1"' in saved and "m001-1.webp" in saved
        assert 'title="このたびは、誠に"' in saved
        assert "</manual>" not in saved

        bad = response.read_text(encoding="utf-8").replace("ありがとうございました。", "ありがとう。")
        assert cmd_next_with_sources(ns) == 0
        response.write_text(
            '<page number="2" img="m001-2.webp"><section type="toc"><head>もくじ</head>'
            "<list><li>◆操作</li></list></section></page>",
            encoding="utf-8",
        )
        # 地の文がずれていても保存し、次のページへ進める。
        reject = job / "reject.txt"
        reject.write_text(
            '<page number="2" img="m001-2.webp"><section type="toc"><head>目次</head>'
            "<p>◆操作</p></section></page>",
            encoding="utf-8",
        )
        accept_ns.response = str(reject)
        assert cmd_accept(accept_ns) == 0
        saved_mismatch = (out / "RCGSa0000001.xml").read_text(encoding="utf-8")
        assert 'number="2"' in saved_mismatch and "目次" in saved_mismatch

        assert cmd_next_with_sources(ns) == 0
        response.write_text(
            '<page number="3" img="m002.webp"><section><p>第二冊</p></section></page>',
            encoding="utf-8",
        )
        accept_ns.response = str(response)
        assert cmd_accept(accept_ns) == 0

        assert cmd_next_with_sources(ns) == 0
        status = read_status(job)
        assert status["blank"] == "1" and status["is_last"] == "1"
        assert cmd_accept_blank(argparse.Namespace(job_dir=str(job), output_dir=str(out))) == 0
        final = (out / "RCGSa0000001.xml").read_text(encoding="utf-8")
        assert final.strip().endswith("</manual>")
        assert final.count("<page ") == 4
        assert cmd_next_with_sources(ns) == 2

        truncated = '<page number="1" img="a.webp"><section><p>途中'
        pages, is_trunc = extract_complete_pages(truncated)
        assert pages == [] and is_trunc
        whole, is_trunc = extract_complete_pages(truncated + "</p></section></page>")
        assert len(whole) == 1 and not is_trunc

        misplaced = (
            '<page number="1" img="a.webp"><section type="gameplay">'
            "<p>前</p><head>見出し</head><p>後</p></section></page>"
        )
        placed, placed_message = validate_page(misplaced)
        assert placed_message is None and placed is not None
        assert [child.tag for child in placed] == ["section", "section"]
        assert element_text(placed) == "前見出し後"
        assert placed[1][0].tag == "head"
        captioned = '<page number="1" img="a.webp"><section><p><caption>戦闘開始</caption></p></section></page>'
        unwrapped, unwrap_message = validate_page(captioned)
        assert unwrap_message is None and unwrapped is not None
        assert element_text(unwrapped) == "戦闘開始"
        assert "caption" not in ET.tostring(unwrapped, encoding="unicode")
        bare = '<page number="1" img="a.webp"><section><p>倒します。!</</p></section></page>'
        fixed, fixed_message = validate_page(bare)
        assert fixed_message is None and fixed is not None
        assert "!</" in element_text(fixed)
        assert xsd_error_for_pages(
            [ET.tostring(placed, encoding="unicode")],
            default_schema(),
        ) is None
        nested = (
            '<page number="1" img="a.webp"><section type="control">'
            "<p><partOfDevice>Aボタン</partOfDevice></p></section></page>"
        )
        _el, message = validate_page(nested)
        assert message and "partOfDevice" in message
        direct = '<page number="1" img="a.webp"><section>直書き</section></page>'
        _el, message = validate_page(direct)
        assert message and "直下" in message
        noisy = src / "RCGSa0000009_m001_001_converted.xml"
        noisy.write_text(
            "<OCRDATASET><PAGE IMAGENAME=\"noisy.webp\">"
            '<LINE TYPE="本文" STRING="本文だけ"/>'
            '<SHAPE><POLYGON POINTS="1,2,3,4,9,9"/></SHAPE>'
            '<BLOCK TYPE="図版" X="10" Y="20" WIDTH="30" HEIGHT="40"/>'
            "</PAGE></OCRDATASET>",
            encoding="utf-8",
        )
        _image, noisy_lines = read_page_xml(noisy)
        assert noisy_lines == [("本文", "本文だけ")]
        rendered_noisy = "".join(text for _kind, text in noisy_lines)
        assert "POLYGON" not in rendered_noisy and "1,2,3,4" not in rendered_noisy
        schema = default_schema()
        assert xsd_error_for_pages(
            ['<page number="1" img="a.webp"><section type="blank"/></page>'],
            schema,
        ) is None
        invalid = xsd_error_for_pages(
            ['<page number="1" img="a.webp"><section type="not-a-type"><p>本文だけ</p></section></page>'],
            schema,
        )
        assert invalid and "not-a-type" in invalid
        body = build_generate_body(
            stable_system_instruction("RULES", "<xs:schema/>"), "PAGE", 0.0, "gemini-2.5-flash"
        )
        assert body["generationConfig"]["temperature"] == 0.0
        assert body["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}
        flash3 = build_generate_body("RULES", "PAGE", 0.0, "gemini-3.8-flash")
        assert flash3["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}
        lite = build_generate_body("RULES", "PAGE", 0.0, "gemini-3.1-flash-lite")
        assert lite["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "minimal"}
        assert body["systemInstruction"]["parts"][0]["text"].startswith("RULES")
        assert "PAGE" not in body["systemInstruction"]["parts"][0]["text"]
        assert body["contents"][0]["parts"][0]["text"] == "PAGE"
        _ = bad
    finally:
        shutil.rmtree(root)
    print("self-test ok")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ゲームマニュアルマークアップの補助処理")
    sub = parser.add_subparsers(dest="command", required=True)

    manifest = sub.add_parser("manifest")
    manifest.add_argument("--input", required=True)
    manifest.add_argument("--out", required=True)
    manifest.set_defaults(func=cmd_manifest)

    summary = sub.add_parser("summary")
    summary.add_argument("--manifest", required=True)
    summary.add_argument("--output-dir", required=True)
    summary.add_argument("--only", default="")
    summary.add_argument("--from-game", default="")
    summary.set_defaults(func=cmd_summary)

    nxt = sub.add_parser("next")
    nxt.add_argument("--manifest", required=True)
    nxt.add_argument("--output-dir", required=True)
    nxt.add_argument("--job-dir", required=True)
    nxt.add_argument("--only", default="")
    nxt.add_argument("--from-game", default="")
    nxt.add_argument("--pages-per-call", default="1")
    nxt.set_defaults(func=cmd_next_with_sources)

    accept = sub.add_parser("accept")
    accept.add_argument("--job-dir", required=True)
    accept.add_argument("--output-dir", required=True)
    accept.add_argument("--response", required=True)
    accept.add_argument("--continue-output", action="store_true")
    accept.add_argument("--schema", default="")
    accept.add_argument("--model", default="")
    accept.set_defaults(func=cmd_accept)

    blank = sub.add_parser("accept-blank")
    blank.add_argument("--job-dir", required=True)
    blank.add_argument("--output-dir", required=True)
    blank.add_argument("--schema", default="")
    blank.add_argument("--model", default="")
    blank.set_defaults(func=cmd_accept_blank)

    generate = sub.add_parser("generate")
    generate.add_argument("--system", required=True)
    generate.add_argument("--schema", required=True)
    generate.add_argument("--user", required=True)
    generate.add_argument("--out", required=True)
    generate.add_argument("--err", required=True)
    generate.add_argument("--model", required=True)
    generate.add_argument("--temperature", default="0")
    generate.add_argument("--timeout", default="300")
    generate.set_defaults(func=cmd_generate)

    test = sub.add_parser("self-test")
    test.set_defaults(func=lambda _args: self_test())
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
