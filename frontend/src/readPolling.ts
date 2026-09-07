type Visibility = Pick<Document, 'hidden' | 'addEventListener' | 'removeEventListener'>;
type Timer = ReturnType<typeof setTimeout>;
type Clock = { schedule: (callback: () => void, delay: number) => Timer; cancel: (timer: Timer) => void };

export function historyPollInterval(hours: number, resolutionSeconds = 0): number {
  if (resolutionSeconds >= 86400) return 60000;
  return hours > 24 ? 30000 : 10000;
}

// The owner mounts this only for the visible application section. Browser-tab
// visibility also cancels pending reads, and returning triggers one fresh read.
export function startVisiblePolling(update: (signal: AbortSignal) => Promise<void>, interval: () => number,
  visibility: Visibility = document,
  clock: Clock = { schedule: (callback, delay) => setTimeout(callback, delay), cancel: timer => clearTimeout(timer) }): () => void {
  let stopped = false;
  let timer: Timer | undefined;
  let active: AbortController | null = null;
  const clear = () => {
    if (timer !== undefined) { clock.cancel(timer); timer = undefined; }
    active?.abort();
    active = null;
  };
  async function run() {
    if (stopped || visibility.hidden || active) return;
    const request = new AbortController();
    active = request;
    try { await update(request.signal); }
    catch { /* The caller reports read failures; a failed read remains eligible for retry. */ }
    finally {
      if (active === request) active = null;
      if (!stopped && !visibility.hidden && !request.signal.aborted) timer = clock.schedule(() => void run(), interval());
    }
  }
  const onVisibility = () => { clear(); if (!visibility.hidden) void run(); };
  visibility.addEventListener('visibilitychange', onVisibility);
  void run();
  return () => { stopped = true; clear(); visibility.removeEventListener('visibilitychange', onVisibility); };
}
