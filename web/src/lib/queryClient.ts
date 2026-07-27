import { QueryClient } from "@tanstack/react-query"
import { isTransientError } from "./api"

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 60_000,
      refetchOnWindowFocus: false,
      // Retry only failures that can actually resolve themselves — network drops,
      // 5xx, 408/429. This used to be a flat `retry: false`, which was aimed at the
      // real problem that the built-in default retried a 401 three futile times
      // while the demo-session interceptor was already redirecting; but it also
      // gave up on genuine network blips, so a phone that briefly lost signal
      // showed a dead panel until manual reload. isTransientError draws the line
      // where it belongs instead of disabling retry wholesale.
      retry: (failureCount, error) => failureCount < 3 && isTransientError(error),
      // Caps at 5s rather than the library default's 30s — every query here is
      // small and local-network, so a half-minute backoff reads as "broken".
      retryDelay: (attemptIndex) => Math.min(1000 * 2 ** attemptIndex, 5000),
    },
    mutations: {
      // Deliberately NOT retried. Mutations here are POST/PATCH/DELETE, and a
      // network rejection does not tell us whether the server processed the
      // request before the response was lost — a retry can double-apply it. The
      // chat send is the sharpest case (its tool calls can write HealthNote and
      // Workout rows), which is why that path offers an explicit user-driven
      // Retry instead. See ChatPage.
      retry: false,
    },
  },
})
