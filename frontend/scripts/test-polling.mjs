import assert from "node:assert/strict"
import { mock } from "node:test"

/** Exercise slow responses and disposal without waiting on real wall-clock time. */
export async function assertPolling(startPolling) {
  mock.timers.enable({ apis: ["setTimeout"] })
  try {
    let calls = 0
    let settle
    const refresh = () => {
      calls += 1
      return new Promise((resolve) => { settle = resolve })
    }
    const stop = startPolling(refresh, 1_000)
    assert.equal(calls, 1)
    mock.timers.tick(10_000)
    assert.equal(calls, 1, "a slow response must not overlap another poll")
    settle()
    await Promise.resolve()
    mock.timers.tick(999)
    assert.equal(calls, 1)
    mock.timers.tick(1)
    assert.equal(calls, 2, "the interval starts after the response settles")
    stop()
    settle()
    await Promise.resolve()
    mock.timers.tick(10_000)
    assert.equal(calls, 2, "an in-flight completion must not restart disposed polling")

    const stopBeforeStart = startPolling(refresh, 1_000, false)
    stopBeforeStart()
    mock.timers.tick(1_000)
    assert.equal(calls, 2)

    let failures = 0
    const stopFailures = startPolling(async () => {
      failures += 1
      throw new Error("temporary failure")
    }, 1_000)
    await Promise.resolve()
    mock.timers.tick(1_000)
    assert.equal(failures, 2, "transient errors must not disable polling")
    stopFailures()
    await Promise.resolve()
  } finally {
    mock.timers.reset()
  }
}
