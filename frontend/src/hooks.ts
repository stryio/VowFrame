import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Loads data once and re-fetches every `intervalMs` while `shouldPoll(data)` is true.
 * Returns the latest data, the latest error and a manual `reload`.
 */
export function usePolledResource<T>(
  load: () => Promise<T>,
  deps: unknown[],
  shouldPoll: (data: T) => boolean = () => false,
  intervalMs = 2000,
) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const loadRef = useRef(load);
  const pollRef = useRef(shouldPoll);
  loadRef.current = load;
  pollRef.current = shouldPoll;

  const reload = useCallback(async () => {
    try {
      const next = await loadRef.current();
      setData(next);
      setError(null);
      return next;
    } catch (err) {
      setError(err as Error);
      return null;
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    setData(null);

    const tick = async () => {
      const next = await reload();
      if (cancelled) return;
      // Keep polling after an error so the page recovers once the backend is back.
      if (next === null || pollRef.current(next)) {
        timer = window.setTimeout(tick, intervalMs);
      }
    };
    tick();

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  // Restart polling after a mutation (e.g. new tasks created) even if it had stopped.
  const refresh = useCallback(async () => {
    const next = await reload();
    if (next !== null && pollRef.current(next)) {
      const poll = async () => {
        const again = await reload();
        if (again !== null && pollRef.current(again)) window.setTimeout(poll, intervalMs);
      };
      window.setTimeout(poll, intervalMs);
    }
  }, [reload, intervalMs]);

  return { data, error, setData, refresh };
}

const CURRENT_PROJECT_KEY = "vowframe.currentProjectId";

export function readCurrentProjectId(): string | null {
  try {
    return localStorage.getItem(CURRENT_PROJECT_KEY);
  } catch {
    return null;
  }
}

export function writeCurrentProjectId(id: string | null) {
  try {
    if (id) localStorage.setItem(CURRENT_PROJECT_KEY, id);
    else localStorage.removeItem(CURRENT_PROJECT_KEY);
  } catch {
    // Remembering the last project is a convenience only.
  }
}
