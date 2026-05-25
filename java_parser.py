"""Tree-sitter Java parsing — shared by Phase 2 (pdf_generator.py) and Phase 3 (qa.py).
Replaces the previous regex-based parsing with proper AST traversal so we correctly
handle inner classes, package-private methods, complex generics, and constructors.
"""

from dataclasses import dataclass, field

import tree_sitter_java
from tree_sitter import Language, Parser


_LANGUAGE = Language(tree_sitter_java.language())


def _new_parser() -> Parser:
    return Parser(_LANGUAGE)


def _text(node, src_bytes: bytes) -> str:
    return src_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _child_by_type(node, types: set[str]):
    for c in node.children:
        if c.type in types:
            return c
    return None


_TYPE_NODE_KINDS = {
    "class_declaration",
    "interface_declaration",
    "enum_declaration",
    "record_declaration",
    "annotation_type_declaration",
}

_TYPE_NAME_KINDS = {
    "type_identifier",
    "scoped_type_identifier",
    "generic_type",
}

_METHOD_NODE_KINDS = {"method_declaration", "constructor_declaration"}


@dataclass
class ParsedMethod:
    name: str
    return_type: str  # empty for constructors
    parameters: str   # comma-separated formal params, parens stripped
    start_byte: int
    end_byte: int
    is_constructor: bool

    def signature(self) -> str:
        """Format used by pdf_generator's overview prompt."""
        if self.is_constructor:
            return f"{self.name}({self.parameters})"
        return f"{self.name}({self.parameters}) : {self.return_type or 'void'}"


@dataclass
class ParsedClass:
    name: str
    kind: str           # class | interface | enum | record | annotation
    extends: str | None
    implements: list[str] = field(default_factory=list)
    methods: list[ParsedMethod] = field(default_factory=list)
    start_byte: int = 0
    end_byte: int = 0


def _extract_package(root, src_bytes: bytes) -> str | None:
    for c in root.children:
        if c.type == "package_declaration":
            for sub in c.children:
                if sub.type in ("scoped_identifier", "identifier"):
                    return _text(sub, src_bytes)
    return None


def _iter_type_declarations(node):
    """Yield class/interface/enum/record declarations at any nesting depth."""
    if node.type in _TYPE_NODE_KINDS:
        yield node
    for child in node.children:
        yield from _iter_type_declarations(child)


def _parse_method(method_node, src_bytes: bytes) -> ParsedMethod | None:
    is_constructor = method_node.type == "constructor_declaration"
    name = None
    return_type = ""
    parameters = ""

    for c in method_node.children:
        if c.type == "identifier" and name is None:
            name = _text(c, src_bytes)
        elif c.type == "formal_parameters":
            raw = _text(c, src_bytes).strip()
            if raw.startswith("(") and raw.endswith(")"):
                raw = raw[1:-1]
            parameters = " ".join(raw.split())
        elif not is_constructor and return_type == "" and c.type in {
            "void_type",
            "integral_type",
            "floating_point_type",
            "boolean_type",
            "type_identifier",
            "scoped_type_identifier",
            "generic_type",
            "array_type",
        }:
            return_type = _text(c, src_bytes).strip()

    if not name:
        return None
    return ParsedMethod(
        name=name,
        return_type=return_type,
        parameters=parameters,
        start_byte=method_node.start_byte,
        end_byte=method_node.end_byte,
        is_constructor=is_constructor,
    )


def _kind_from_node(node_type: str) -> str:
    return {
        "class_declaration": "class",
        "interface_declaration": "interface",
        "enum_declaration": "enum",
        "record_declaration": "record",
        "annotation_type_declaration": "annotation",
    }.get(node_type, "class")


def _body_node(type_node):
    return _child_by_type(type_node, {
        "class_body", "interface_body", "enum_body",
        "record_body", "annotation_type_body",
    })


def _extract_extends(type_node, src_bytes: bytes) -> str | None:
    sc = _child_by_type(type_node, {"superclass"})
    if not sc:
        return None
    type_child = _child_by_type(sc, _TYPE_NAME_KINDS)
    if type_child:
        return _text(type_child, src_bytes)
    return None


def _extract_implements(type_node, src_bytes: bytes) -> list[str]:
    si = _child_by_type(type_node, {"super_interfaces", "extends_interfaces"})
    if not si:
        return []
    type_list = _child_by_type(si, {"type_list"})
    if not type_list:
        return []
    return [_text(c, src_bytes) for c in type_list.children if c.type in _TYPE_NAME_KINDS]


@dataclass
class ParsedFile:
    package: str
    classes: list[ParsedClass]
    src_bytes: bytes


def parse_java_source(src_text: str) -> ParsedFile:
    """Parse one Java source file and return its package + class metadata."""
    src_bytes = src_text.encode("utf-8")
    tree = _new_parser().parse(src_bytes)
    root = tree.root_node

    package = _extract_package(root, src_bytes) or "(default)"
    classes: list[ParsedClass] = []

    for type_node in _iter_type_declarations(root):
        name_node = _child_by_type(type_node, {"identifier"})
        if not name_node:
            continue
        name = _text(name_node, src_bytes)

        methods: list[ParsedMethod] = []
        body = _body_node(type_node)
        if body:
            for c in body.children:
                if c.type in _METHOD_NODE_KINDS:
                    parsed = _parse_method(c, src_bytes)
                    if parsed:
                        methods.append(parsed)

        classes.append(ParsedClass(
            name=name,
            kind=_kind_from_node(type_node.type),
            extends=_extract_extends(type_node, src_bytes),
            implements=_extract_implements(type_node, src_bytes),
            methods=methods,
            start_byte=type_node.start_byte,
            end_byte=type_node.end_byte,
        ))

    return ParsedFile(package=package, classes=classes, src_bytes=src_bytes)


def chunk_by_methods(src_text: str) -> list[str]:
    """Return chunks: file header (imports + class declaration line) + each method body.
    Used by qa.py for BM25 indexing. Caller applies max_chars cap downstream."""
    src_bytes = src_text.encode("utf-8")
    tree = _new_parser().parse(src_bytes)

    method_ranges: list[tuple[int, int]] = []

    def walk(node):
        if node.type in _METHOD_NODE_KINDS:
            method_ranges.append((node.start_byte, node.end_byte))
        for c in node.children:
            walk(c)
    walk(tree.root_node)
    method_ranges.sort()

    if not method_ranges:
        return [src_text] if src_text.strip() else []

    chunks: list[str] = []
    header = src_bytes[:method_ranges[0][0]].decode("utf-8", errors="replace").strip()
    if header:
        chunks.append(header)
    for start, end in method_ranges:
        piece = src_bytes[start:end].decode("utf-8", errors="replace").strip()
        if piece:
            chunks.append(piece)
    return chunks
