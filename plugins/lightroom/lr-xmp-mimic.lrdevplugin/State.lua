-- Session-only bridge and command state.
return {
    -- Bridge state
    bridgeRunning = false,
    bridgeGeneration = 0,
    bridgeLastError = nil,
    -- Captured develop-settings baselines, keyed by photo uuid.
    baselines = {},
}
