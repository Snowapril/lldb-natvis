"""Dataclass model mirroring the natvis XML schema (document order preserved)."""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

PRIORITY_RANK = {
    "Low": 0,
    "MediumLow": 1,
    "Medium": 2,
    "MediumHigh": 3,
    "High": 4,
}


@dataclass
class ViewConstraint:
    include_view: Optional[str] = None
    exclude_view: Optional[str] = None

    def allows(self, view):
        # view is "" for the default view
        if self.include_view is not None:
            if view not in [v.strip() for v in self.include_view.split(";")]:
                return False
        if self.exclude_view is not None:
            if view in [v.strip() for v in self.exclude_view.split(";")]:
                return False
        return True


@dataclass
class CondExpr:
    """An expression element that may carry Condition/Optional attributes.

    Natvis allows several sibling elements (<Size>, <ValuePointer>, ...) where
    the first whose Condition holds wins."""
    expr: str
    condition: Optional[str] = None
    optional: bool = False


@dataclass
class DisplayStringNode(ViewConstraint):
    text: str = ""
    condition: Optional[str] = None
    optional: bool = False


@dataclass
class StringViewNode(ViewConstraint):
    expr: str = ""
    condition: Optional[str] = None
    optional: bool = False


# ---------------------------------------------------------------- Expand nodes

@dataclass
class ExpandNode(ViewConstraint):
    condition: Optional[str] = None
    optional: bool = False


@dataclass
class ItemNode(ExpandNode):
    name: str = ""
    expr: str = ""


@dataclass
class ArrayItemsNode(ExpandNode):
    sizes: List[CondExpr] = field(default_factory=list)
    value_pointers: List[CondExpr] = field(default_factory=list)
    direction: str = "Forward"          # Forward | Backward
    rank_expr: Optional[str] = None
    lower_bound_expr: Optional[str] = None


@dataclass
class IndexListItemsNode(ExpandNode):
    sizes: List[CondExpr] = field(default_factory=list)
    value_nodes: List[CondExpr] = field(default_factory=list)   # contain $i


@dataclass
class LinkedListItemsNode(ExpandNode):
    sizes: List[CondExpr] = field(default_factory=list)
    head_pointer: str = ""
    next_pointer: str = ""
    value_node: str = ""
    value_node_name: Optional[str] = None    # Name attr on <ValueNode>, may contain {expr}
    value_node_condition: Optional[str] = None


@dataclass
class TreeItemsNode(ExpandNode):
    sizes: List[CondExpr] = field(default_factory=list)
    head_pointer: str = ""
    left_pointer: str = ""
    right_pointer: str = ""
    value_node: str = ""
    value_node_name: Optional[str] = None
    value_node_condition: Optional[str] = None


@dataclass
class ExpandedItemNode(ExpandNode):
    expr: str = ""


@dataclass
class SyntheticNode(ExpandNode):
    name: str = ""
    expression: Optional[str] = None     # <Expression> child (VS shows it as value)
    display_strings: List[DisplayStringNode] = field(default_factory=list)
    string_views: List[StringViewNode] = field(default_factory=list)
    expand: List["ExpandNode"] = field(default_factory=list)


# ------------------------------------------------- CustomListItems statements

@dataclass
class SVar:
    name: str
    init: str


@dataclass
class SSize:
    expr: str
    condition: Optional[str] = None


@dataclass
class SLoop:
    condition: Optional[str]
    body: List[object]


@dataclass
class SBreak:
    condition: Optional[str] = None


@dataclass
class SIf:
    # list of (condition_or_None, body); the None-condition branch is <Else>
    branches: List[Tuple[Optional[str], List[object]]] = field(default_factory=list)


@dataclass
class SExec:
    code: str
    condition: Optional[str] = None


@dataclass
class SItem:
    expr: str
    name: Optional[str] = None           # may contain {expr}
    condition: Optional[str] = None


@dataclass
class CustomListItemsNode(ExpandNode):
    max_items: Optional[int] = None
    variables: List[SVar] = field(default_factory=list)
    block: List[object] = field(default_factory=list)


# ---------------------------------------------------------------------- Type

@dataclass
class Intrinsic:
    name: str
    expression: str
    params: List[Tuple[str, str]] = field(default_factory=list)   # (name, type)


@dataclass
class NatvisType:
    names: List[str]                     # Name + AlternativeType names
    priority: str = "Medium"
    inheritable: bool = True
    display_strings: List[DisplayStringNode] = field(default_factory=list)
    string_views: List[StringViewNode] = field(default_factory=list)
    expand: List[ExpandNode] = field(default_factory=list)
    intrinsics: List[Intrinsic] = field(default_factory=list)
    source_file: str = ""
    source_order: int = 0

    @property
    def priority_rank(self):
        return PRIORITY_RANK.get(self.priority, 2)

    @property
    def has_expand(self):
        return bool(self.expand)

    @property
    def has_summary(self):
        return bool(self.display_strings or self.string_views)


@dataclass
class NatvisFile:
    path: str
    types: List[NatvisType] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
