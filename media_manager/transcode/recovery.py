"""
What to do with a job found half-done after a worker crash/restart (pure).

The swap is two renames, each announced to the server first:
    phase=swapping:  source -> backup (.mm-orig),  then  temp -> output
    phase=swapped:   both renames done, /complete not yet acknowledged
Before 'swapping' nothing in the library changed except an optional temp
file next to the source. The server's job row (paths + output hash) is the
write-ahead log, so recovery only needs what is on disk now.
"""
from typing import Dict, Optional

CLEANUP_RETRY = "cleanup_retry"  # delete temp/scratch leftovers, requeue (original untouched)
FINISH_SWAP = "finish_swap"      # rename temp -> output, then complete
COMPLETE = "complete"            # new file in place: report completion
ROLLBACK = "rollback"            # restore backup -> source, drop the new file, requeue
MANUAL = "manual"                # state makes no sense: touch nothing, flag for a human

PRE_SWAP_PHASES = {"queued", "preparing", "encoding", "verifying", "installing"}


def plan_recovery(phase: str, exists: Dict[str, bool], same_path: bool, hash_ok: Optional[bool]) -> str:
    """
    exists: {'source', 'backup', 'temp', 'output'} -> whether that path is on disk
            (with same_path, 'source' and 'output' are the same path).
    hash_ok: does the temp file (or, if no temp, the output) match the hash
             recorded at install time? None = unknown (never trust it).
    """
    backup, temp = exists.get("backup", False), exists.get("temp", False)
    output = exists.get("output", False)
    source = output if same_path else exists.get("source", False)

    if phase in PRE_SWAP_PHASES:
        if backup:
            return MANUAL  # a backup without a swap having been announced - don't guess
        return CLEANUP_RETRY if source else MANUAL

    # swapping / swapped
    if not backup:
        # First rename never happened: the original is still in place.
        return CLEANUP_RETRY if source else MANUAL
    if temp and not output:
        return FINISH_SWAP if hash_ok else ROLLBACK
    if output and not temp:
        return COMPLETE if hash_ok else ROLLBACK
    if not output and not temp:
        return ROLLBACK  # new file lost: put the original back
    return MANUAL  # both temp and output present: shouldn't happen
