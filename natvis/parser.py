"""Lenient natvis XML parser: XML -> model. Unknown elements are logged and
skipped, never fatal, so real-world natvis files (godot, UE, ...) always load."""

import re
import xml.etree.ElementTree as ET

_XMLDECL_RE = re.compile(r"<\?xml[^>]*\?>")

from . import log
from .model import (
    ArrayItemsNode, CondExpr, CustomListItemsNode, DisplayStringNode,
    ExpandedItemNode, IndexListItemsNode, Intrinsic, ItemNode,
    LinkedListItemsNode, NatvisFile, NatvisType, SBreak, SExec, SIf, SItem,
    SLoop, SSize, StringViewNode, SVar, SyntheticNode, TreeItemsNode,
)

_IGNORED_TYPE_CHILDREN = {
    "Version", "UIVisualizer", "MostDerivedType", "SmartPointer",
    "CustomVisualizer",
}


def _strip_ns(tree):
    for elem in tree.iter():
        if isinstance(elem.tag, str) and "}" in elem.tag:
            elem.tag = elem.tag.split("}", 1)[-1]


def _bool(value, default=False):
    if value is None:
        return default
    return value.strip().lower() in ("true", "1")


def _text(elem):
    return (elem.text or "").strip()


def _view_attrs(elem, node):
    node.include_view = elem.get("IncludeView")
    node.exclude_view = elem.get("ExcludeView")


def _cond_attrs(elem, node):
    node.condition = elem.get("Condition")
    node.optional = _bool(elem.get("Optional"))
    _view_attrs(elem, node)


def _cond_exprs(parent, tag):
    out = []
    for child in parent.findall(tag):
        out.append(CondExpr(
            expr=_text(child),
            condition=child.get("Condition"),
            optional=_bool(child.get("Optional")),
        ))
    return out


def parse_file(path):
    """Parse a .natvis file. Returns NatvisFile; parse problems land in .errors."""
    nf = NatvisFile(path=path)
    try:
        tree = ET.parse(path)
    except ET.ParseError:
        # VS tolerates comments before the <?xml?> declaration; retry with
        # the misplaced declaration stripped.
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                text = f.read()
            text = _XMLDECL_RE.sub("", text, count=1)
            tree = ET.ElementTree(ET.fromstring(text))
        except (ET.ParseError, OSError, UnicodeDecodeError) as exc:
            nf.errors.append("cannot parse %s: %s" % (path, exc))
            return nf
    except OSError as exc:
        nf.errors.append("cannot parse %s: %s" % (path, exc))
        return nf
    root = tree.getroot()
    _strip_ns(tree)
    if root.tag != "AutoVisualizer":
        nf.errors.append("%s: root element is <%s>, expected <AutoVisualizer>"
                         % (path, root.tag))
        return nf
    order = 0
    for elem in root:
        if elem.tag == "Type":
            try:
                ntype = _parse_type(elem, nf)
            except Exception as exc:      # lenient: skip broken Type, keep going
                nf.errors.append("%s: <Type Name=%r>: %s"
                                 % (path, elem.get("Name"), exc))
                continue
            if ntype is not None:
                ntype.source_file = path
                ntype.source_order = order
                order += 1
                nf.types.append(ntype)
        else:
            log.debug("%s: ignoring top-level <%s>", path, elem.tag)
    return nf


def _parse_type(elem, nf):
    name = elem.get("Name")
    if not name:
        nf.errors.append("<Type> without Name attribute")
        return None
    names = [name]
    for alt in elem.findall("AlternativeType"):
        alt_name = alt.get("Name")
        if alt_name:
            names.append(alt_name)
    ntype = NatvisType(
        names=names,
        priority=elem.get("Priority", "Medium"),
        inheritable=_bool(elem.get("Inheritable"), True),
    )
    for child in elem:
        tag = child.tag
        if tag == "DisplayString":
            node = DisplayStringNode(text=child.text or "")
            _cond_attrs(child, node)
            ntype.display_strings.append(node)
        elif tag == "StringView":
            node = StringViewNode(expr=_text(child))
            _cond_attrs(child, node)
            ntype.string_views.append(node)
        elif tag == "Expand":
            ntype.expand = _parse_expand(child, nf)
        elif tag == "Intrinsic":
            intr = Intrinsic(
                name=child.get("Name", ""),
                expression=child.get("Expression", ""),
            )
            for param in child.findall("Parameter"):
                intr.params.append((param.get("Name", ""), param.get("Type", "")))
            if intr.name and intr.expression:
                ntype.intrinsics.append(intr)
        elif tag == "AlternativeType":
            pass
        elif tag in _IGNORED_TYPE_CHILDREN:
            log.debug("ignoring <%s> in Type %s", tag, name)
        else:
            log.debug("unknown element <%s> in Type %s", tag, name)
    return ntype


def _parse_expand(elem, nf):
    out = []
    for child in elem:
        tag = child.tag
        if tag == "Item":
            node = ItemNode(name=child.get("Name", ""), expr=_text(child))
            _cond_attrs(child, node)
            out.append(node)
        elif tag == "ArrayItems":
            node = ArrayItemsNode(
                sizes=_cond_exprs(child, "Size"),
                value_pointers=_cond_exprs(child, "ValuePointer"),
                direction=child.get("Direction", "Forward"),
            )
            rank = child.find("Rank")
            if rank is not None:
                node.rank_expr = _text(rank)
            lower = child.find("LowerBound")
            if lower is not None:
                node.lower_bound_expr = _text(lower)
            direction = child.find("Direction")
            if direction is not None:
                node.direction = _text(direction) or node.direction
            _cond_attrs(child, node)
            out.append(node)
        elif tag == "IndexListItems":
            node = IndexListItemsNode(
                sizes=_cond_exprs(child, "Size"),
                value_nodes=_cond_exprs(child, "ValueNode"),
            )
            _cond_attrs(child, node)
            out.append(node)
        elif tag == "LinkedListItems":
            node = LinkedListItemsNode(sizes=_cond_exprs(child, "Size"))
            head = child.find("HeadPointer")
            nxt = child.find("NextPointer")
            vnode = child.find("ValueNode")
            node.head_pointer = _text(head) if head is not None else "this"
            node.next_pointer = _text(nxt) if nxt is not None else ""
            if vnode is not None:
                node.value_node = _text(vnode)
                node.value_node_name = vnode.get("Name")
                node.value_node_condition = vnode.get("Condition")
            _cond_attrs(child, node)
            out.append(node)
        elif tag == "TreeItems":
            node = TreeItemsNode(sizes=_cond_exprs(child, "Size"))
            for tag2, attr in (("HeadPointer", "head_pointer"),
                               ("LeftPointer", "left_pointer"),
                               ("RightPointer", "right_pointer")):
                sub = child.find(tag2)
                if sub is not None:
                    setattr(node, attr, _text(sub))
            vnode = child.find("ValueNode")
            if vnode is not None:
                node.value_node = _text(vnode)
                node.value_node_name = vnode.get("Name")
                node.value_node_condition = vnode.get("Condition")
            _cond_attrs(child, node)
            out.append(node)
        elif tag == "ExpandedItem":
            node = ExpandedItemNode(expr=_text(child))
            _cond_attrs(child, node)
            out.append(node)
        elif tag == "Synthetic":
            node = SyntheticNode(name=child.get("Name", ""))
            _cond_attrs(child, node)
            expr = child.find("Expression")
            if expr is not None:
                node.expression = _text(expr)
            for ds in child.findall("DisplayString"):
                dnode = DisplayStringNode(text=ds.text or "")
                _cond_attrs(ds, dnode)
                node.display_strings.append(dnode)
            for sv in child.findall("StringView"):
                snode = StringViewNode(expr=_text(sv))
                _cond_attrs(sv, snode)
                node.string_views.append(snode)
            sub_expand = child.find("Expand")
            if sub_expand is not None:
                node.expand = _parse_expand(sub_expand, nf)
            out.append(node)
        elif tag == "CustomListItems":
            node = _parse_custom_list(child, nf)
            _cond_attrs(child, node)
            out.append(node)
        else:
            log.debug("unknown element <%s> in <Expand>", tag)
    return out


def _parse_custom_list(elem, nf):
    node = CustomListItemsNode()
    max_items = elem.get("MaxItemsPerView")
    if max_items:
        try:
            node.max_items = int(max_items)
        except ValueError:
            pass
    stmts = []
    for child in elem:
        if child.tag == "Variable":
            node.variables.append(SVar(
                name=child.get("Name", ""),
                init=child.get("InitialValue", "0"),
            ))
        else:
            stmts.append(child)
    node.block = _parse_stmt_block(stmts, nf)
    return node


def _parse_stmt_block(elems, nf):
    """Parse a statement list, folding sibling If/Elseif/Else runs into SIf."""
    out = []
    i = 0
    while i < len(elems):
        elem = elems[i]
        tag = elem.tag
        if tag == "If":
            branches = [(elem.get("Condition"), _parse_stmt_block(list(elem), nf))]
            i += 1
            while i < len(elems) and elems[i].tag in ("Elseif", "ElseIf", "Else"):
                sib = elems[i]
                cond = sib.get("Condition") if sib.tag != "Else" else None
                branches.append((cond, _parse_stmt_block(list(sib), nf)))
                is_else = sib.tag == "Else"
                i += 1
                if is_else:
                    break
            out.append(SIf(branches=branches))
            continue
        if tag == "Loop":
            out.append(SLoop(condition=elem.get("Condition"),
                             body=_parse_stmt_block(list(elem), nf)))
        elif tag == "Break":
            out.append(SBreak(condition=elem.get("Condition")))
        elif tag == "Exec":
            out.append(SExec(code=_text(elem), condition=elem.get("Condition")))
        elif tag == "Item":
            out.append(SItem(expr=_text(elem), name=elem.get("Name"),
                             condition=elem.get("Condition")))
        elif tag == "Size":
            out.append(SSize(expr=_text(elem), condition=elem.get("Condition")))
        elif tag in ("Elseif", "ElseIf", "Else"):
            # dangling without a preceding <If>; skip
            log.debug("dangling <%s> in CustomListItems", tag)
        else:
            log.debug("unknown element <%s> in CustomListItems", tag)
        i += 1
    return out
