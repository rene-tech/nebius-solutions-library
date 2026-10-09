# Local observer pipe cleanup and append-only restart

`observe.py` is a read-only system/qa GROMACS telemetry collector. This fix
does not restart it, modify admission, terminate a Pod or change a customer
resource. Root owns any later observer replacement.

## Corrected local reader

The observer now shares the corrected `acceptance_subprocess.py` reader with
`verify_concurrency.py`. Each **local** command gets its own process group. Its
`communicate()` task is shielded from the normal timeout; timeout/cancellation
terminates only that command group, including credential-plugin descendants,
then drains both pipes with a bounded cleanup wait. This avoids the previous
deadlock: cancelling `communicate()`, killing just the direct process, and
awaiting `wait()` while a pipe remains buffered or inherited by a descendant.

`read_command` retains its original contract: stdout bytes on success and an
output-free exception on nonzero exit. The observer uses `read_command_result`
to keep stdout, stderr and return code, with the same replacement decoding as
before. Timeout remains `{"error":"observation_timeout"}`; it does not become
zero utilization or a successful empty metadata sample. Cancellation propagates
after local process cleanup. No remote kill command is sent.

Offline tests exercise successful/nonzero results, invalid UTF-8, large output
on both pipes, timeout after a direct parent exits with inherited pipes still
open, cancellation, and append-only/error recording. They launch only short
local Python test processes, never kubectl or a GPU workload.

## Later restart procedure (not executed by this change)

1. Resolve the exact existing observer PID, `/proc/PID/stat` start identity,
   command, output directory and context. Record its current child PIDs. Do not
   use broad `pkill`, select a PID only by an old handoff, or stop the benchmark
   supervisor. Running Python code does not pick up this source edit.
2. Record the last complete successful observation timestamps and append-only
   file byte lengths/prefix hashes in a new private restart receipt. Retain all
   prior errors and any incomplete trailing record; an offline snapshot can
   exclude the partial record with its exact byte count, not rewrite raw data.
3. Ask the exact observer to stop through its output directory's `STOP` marker.
   Check for its `observer-closeout.jsonl` entry and confirmed process exit in
   short bounded polls. An old already-deadlocked process may never reach that
   check. If root explicitly authorizes local termination, act only on the
   verified observer and its task-owned local reader descendants. An old reader
   was not session-isolated: never signal the observer's entire inherited
   process group. Do not send signals or deletion requests to remote Pods.
4. Confirm the old observer and local readers have exited before starting one
   replacement. Preserve the STOP marker by moving it to a unique dated sibling
   only after that confirmation. Never truncate/remove existing JSONL files.
5. Use the corrected source and the **same** output directory. All observation
   writes use append mode. Example, after the previous steps are verified:

   ```sh
   python3 /home/tux/worktrees/fs2-customer-workbenches-20261002/k8s-inference/acceptance/gromacs-mpinat-20261003/observe.py \
     --output /home/tux/secure-handoff/fs2-gromacs-mpinat-20261003/telemetry-c \
     --context nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a \
     --seconds 7200 --interval 10
   ```

   Choose the remaining task duration explicitly; the example is not an
   instruction to launch now. The reader is standard-library-only, so this
   observer does not need the HTTP campaign dependencies or any customer key.
6. Record the new PID/start identity, tested source commit/hash, launch time,
   first successful observation and any startup errors in the new restart
   receipt. Verify that every old file prefix still hashes identically and
   that only new records were appended. Keep the old observer closeout and
   restart receipt; do not overwrite prior evidence snapshots.

## Continuity limits

State in `seen` and `previous` is deliberately process-local. The replacement
will resample topology for still-running Pods. It cannot observe the execution,
utilization or exact disappearance of a Pod that came and went while collection
was unavailable. Record the gap from the last relevant successful old sample
to the first relevant successful new sample. Do not fill that gap with zeroes,
pretend the restart time is exact resource release, or manufacture old/new Pod
disappearance events. Existing durable operation/lifecycle events can supply
independently sourced allocation boundaries where available.

Report snapshots must freeze their own complete JSONL records and cutoff. A
restart appends evidence for subsequent reports; it does not silently revise
the fixed historical report or turn an old missing measurement into a pass.
