import { useEffect, useState } from "react";
import type { Result } from "./api";

/** Load one api.ts result for a page: null while loading, then ready data or an honest "unavailable". */
export function useResult<T>(load: () => Promise<Result<T>>): Result<T> | null {
  const [result, setResult] = useState<Result<T> | null>(null);
  useEffect(() => {
    let alive = true;
    load().then((r) => alive && setResult(r));
    return () => {
      alive = false;
    };
  }, [load]);
  return result;
}
