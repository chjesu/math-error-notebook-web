"""Editable whole-paper candidate set; no candidate becomes formal implicitly."""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class PaperItem:
    item_id: str
    question_text: str
    answer_text: str
    input_version: int = 1
    included: bool = True


class PaperDraft:
    def __init__(self, items: list[PaperItem]) -> None:
        if not items or len({item.item_id for item in items}) != len(items):
            raise ValueError("paper items must be non-empty with unique ids")
        self.items = list(items)
        self.confirmed = False

    def revise(self, item_id: str, *, expected_version: int, question_text: str, answer_text: str) -> PaperItem:
        index, item = self._find(item_id)
        self._version(item, expected_version)
        if not question_text.strip():
            raise ValueError("question_text is required")
        updated = replace(item, question_text=question_text.strip(), answer_text=answer_text, input_version=item.input_version + 1)
        self.items[index] = updated
        return updated

    def exclude(self, item_id: str, *, expected_version: int) -> PaperItem:
        index, item = self._find(item_id)
        self._version(item, expected_version)
        updated = replace(item, included=False, input_version=item.input_version + 1)
        self.items[index] = updated
        return updated

    def split(self, item_id: str, *, expected_version: int, second_id: str, first_text: str, second_text: str) -> tuple[PaperItem, PaperItem]:
        index, item = self._find(item_id)
        self._version(item, expected_version)
        if any(value.item_id == second_id for value in self.items) or not first_text.strip() or not second_text.strip():
            raise ValueError("split requires a new id and two non-empty stems")
        first = replace(item, question_text=first_text.strip(), input_version=item.input_version + 1)
        second = PaperItem(second_id, second_text.strip(), "", 1, item.included)
        self.items[index:index + 1] = [first, second]
        return first, second

    def merge(self, first_id: str, second_id: str, *, first_version: int, second_version: int) -> PaperItem:
        first_index, first = self._find(first_id)
        second_index, second = self._find(second_id)
        self._version(first, first_version)
        self._version(second, second_version)
        if abs(first_index - second_index) != 1:
            raise ValueError("only adjacent paper items can be merged")
        merged = PaperItem(first.item_id, f"{first.question_text}\n{second.question_text}", f"{first.answer_text}\n{second.answer_text}".strip(), max(first.input_version, second.input_version) + 1, first.included or second.included)
        low, high = sorted((first_index, second_index))
        self.items[low:high + 1] = [merged]
        return merged

    def confirm(self, expected: dict[str, int]) -> list[PaperItem]:
        included = [item for item in self.items if item.included]
        if not included or any(not item.question_text.strip() for item in included):
            raise ValueError("confirmed paper must contain complete questions")
        if expected != {item.item_id: item.input_version for item in self.items}:
            raise RuntimeError("input_version_changed")
        self.confirmed = True
        return included

    def _find(self, item_id: str) -> tuple[int, PaperItem]:
        for index, item in enumerate(self.items):
            if item.item_id == item_id:
                return index, item
        raise LookupError("paper item not found")

    @staticmethod
    def _version(item: PaperItem, expected: int) -> None:
        if item.input_version != expected:
            raise RuntimeError("input_version_changed")
