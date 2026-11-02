"""Advisory text and placement checks, independent of take-selection heuristics.

Whole-sequence edit distance is intentional: a final dub must account for
inserted words, including slates and repetitions at either end of the audio.
"""
import math
from .human import tokens


def compare_text(expected, recognized, aliases=None):
    """Return normalized token differences; ASR disagreements require review.

    Unlike the human-recording aligner, leading/trailing words are not free.
    Keep the original strings in the calling report; normalization is for
    comparison only. Han characters use the existing skill's tokenization.
    """
    reference, heard = tokens(expected, aliases), tokens(recognized, aliases)
    n, m = len(reference), len(heard)
    if n*m > 4_000_000:
        raise ValueError("Final QA text group too large; split the continuous block")
    moves = [bytearray(m+1) for _ in range(n+1)]
    moves[0][1:] = bytes([2])*m
    previous = list(range(m+1))
    for i, word in enumerate(reference, 1):
        current = [i]
        moves[i][0] = 1
        for j, other in enumerate(heard, 1):
            choices = (previous[j-1]+(word != other), previous[j]+1, current[j-1]+1)
            move = min(range(3), key=choices.__getitem__)
            current.append(choices[move])
            moves[i][j] = move
        previous = current
    i, j = n, m
    differences = []
    counts = {"insertions": 0, "deletions": 0, "substitutions": 0}
    while i or j:
        move = moves[i][j]
        if move == 0:
            i -= 1; j -= 1
            if reference[i] == heard[j]:
                continue
            kind = "substitutions"
            item = {"expected": reference[i], "recognized": heard[j]}
        elif move == 1:
            i -= 1
            kind = "deletions"
            item = {"expected": reference[i], "recognized": None}
        else:
            j -= 1
            kind = "insertions"
            item = {"expected": None, "recognized": heard[j]}
        counts[kind] += 1
        differences.append({"kind": kind, "expected_index": i, "recognized_index": j, **item})
    reasons = [kind for kind, count in counts.items() if count]
    return {"reference_tokens": n, "recognized_tokens": m,
            "token_error_rate": sum(counts.values())/max(1, n),
            "error_counts": counts, "differences": list(reversed(differences)),
            "review": bool(reasons), "review_reasons": reasons}


def placement_review(source_cues, edits, placements):
    """Report placement drift, not ASR-measured word timing or lip sync.

    Thresholds are review hints, separate from the renderer's hard tempo
    and music-boundary limits. Short translations can leave natural silence.
    """
    thresholds = {"start_shift_seconds": .5, "end_overrun_seconds": .8,
                  "tempo_change_fraction": .1}
    source = {str(c["id"]): c for c in source_cues}
    selected = {str(c["id"]): c for c in edits["cues"]}
    checks = []
    for c in placements:
        original, edit = source[str(c["id"])], selected[str(c["id"])]
        start = float(c["output_start"]); end = float(c["output_end"])
        source_shift = start-float(original["start"])
        anchor_shift = start-float(edit["anchor"])
        overrun = max(0., end-float(original["end"]))
        tempo = float(c["tempo"])
        if not all(math.isfinite(x) for x in (start, end, source_shift, anchor_shift, overrun, tempo)):
            raise ValueError("Non-finite placement timing")
        reasons = []
        if abs(source_shift) > thresholds["start_shift_seconds"]+1e-9:
            reasons.append("source_start_shift")
        if abs(anchor_shift) > thresholds["start_shift_seconds"]+1e-9:
            reasons.append("edit_anchor_shift")
        if overrun > thresholds["end_overrun_seconds"]+1e-9:
            reasons.append("source_end_overrun")
        if abs(tempo-1.) > thresholds["tempo_change_fraction"]+1e-9:
            reasons.append("tempo_change")
        checks.append({"id": c["id"], "source_start": original["start"], "source_end": original["end"],
                       "anchor": edit["anchor"], "output_start": start, "output_end": end,
                       "source_start_shift_seconds": source_shift, "anchor_shift_seconds": anchor_shift,
                       "source_end_overrun_seconds": overrun, "tempo": tempo,
                       "review": bool(reasons), "review_reasons": reasons})
    return {"thresholds": thresholds, "cue_checks": checks,
            "flagged_cues": sum(c["review"] for c in checks)}
