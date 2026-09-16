const terminal = new Set(['completed', 'failed', 'aborted']);

export function submissionState(run, selectedRole, busy = false) {
  const ended = !run || terminal.has(run.status);
  const current = run?.status === 'takeover' && run.state.takeover_role === run.state.next_role;
  return {
    blocked: ended || busy,
    canRecord: !ended && !busy && current,
    canSay: !ended && !busy && current && run.state.takeover_role === selectedRole,
  };
}

export class CommandGate {
  busy = false;
  async run(work, changed = () => {}) {
    if (this.busy) return false;
    this.busy = true;
    try { changed(); await work(); return true; }
    finally { this.busy = false; changed(); }
  }
}

export function newestRun(previous, incoming) {
  return previous?.id === incoming.id && previous.version > incoming.version ? previous : incoming;
}
