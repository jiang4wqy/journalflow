from __future__ import annotations

import re


def words(text: str):
    return set(re.findall(r"[a-z]{2,}", text.casefold()))


def numbers(text: str):
    return set(re.findall(r"(?<![\w.])[-+]?\d+(?:\.\d+)?%?(?![\w.])", text.replace(",", "")))


def assess(native: str, markdown: str, blocks: list[dict]):
    expected, actual = words(native), words(markdown)
    recall = len(expected & actual) / len(expected) if len(expected) >= 30 else None
    tables = [block for block in blocks if block.get("block_label") == "table"]
    formulas = [block for block in blocks if block.get("block_label") == "display_formula"]
    expected_numbers, actual_numbers = numbers(native), numbers(markdown)
    numeric = (len(expected_numbers & actual_numbers) / len(expected_numbers)
               if tables and len(expected_numbers) >= 40 else None)
    flags = []
    reasons = []
    if len(native.strip()) > 500 and len(markdown.strip()) < max(100, len(native.strip()) * .35):
        flags.append("very_little_visual_output")
    if recall is not None and recall < .80:
        flags.append("native_word_recall_below_0_80")
    if numeric is not None and numeric < .80:
        flags.append("table_numeric_recall_below_0_80")
    if len(markdown) > 35000:
        flags.append("possible_repetition_or_truncation")
    if len(expected) < 30 and len(markdown.strip()) > 200:
        flags.append("insufficient_native_text_for_automatic_comparison")
    if any(len(re.findall(r"<t[dh](?:\s|>)", block.get("block_content", ""))) >= 200 for block in tables):
        reasons.append("dense_table")
    if len(formulas) >= 5 and sum(len(block.get("block_content", "")) for block in formulas) >= 1200:
        reasons.append("complex_formulas")
    return dict(native_characters=len(native), markdown_characters=len(markdown),
                native_word_recall=round(recall, 4) if recall is not None else None,
                native_numeric_recall=round(numeric, 4) if numeric is not None else None,
                review_flags=flags, refinement_reasons=flags + reasons)


def select_refinement(initial: dict, candidate: dict):
    if len(candidate["review_flags"]) > len(initial["review_flags"]):
        return False
    tolerance = .02 if len(candidate["review_flags"]) < len(initial["review_flags"]) else 0
    if ("very_little_visual_output" in initial["review_flags"] and not tolerance and
        candidate["markdown_characters"] < initial["markdown_characters"]):
        return False
    for metric in ("native_word_recall", "native_numeric_recall"):
        before, after = initial.get(metric), candidate.get(metric)
        if before is not None and (after is None or after < before - tolerance):
            return False
    if (initial["markdown_characters"] > 500 and
        candidate["markdown_characters"] < initial["markdown_characters"] * .65 and
        "possible_repetition_or_truncation" not in initial["review_flags"]):
        return False
    return bool(candidate["markdown_characters"])
