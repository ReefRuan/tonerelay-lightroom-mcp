-- Session-only bridge and command state.
return {
    -- Bridge state
    bridgeRunning = false,
    bridgeGeneration = 0,
    -- Task tokens outlive stop/start only while an old operation is in flight.
    bridgeTask = nil,
    bridgeOperationTask = nil,
    bridgeOperationGeneration = nil,
    bridgeLastError = nil,
    -- Captured develop-settings baselines, keyed by photo uuid.
    baselines = {},
}
