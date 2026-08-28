/**
 * MoP (Master of Panes) — Core type definitions
 *
 * Three concerns:
 * 1. HTTP hook events from Claude Code slots
 * 2. Slot state tracking
 * 3. MCP tool interfaces for PM queries
 */
export const DEFAULT_CONFIG = {
    httpPort: 3100,
    mcpTransport: "stdio",
    dbPath: "./data/mop.db",
    pmPaneAddress: "0:0.0",
    slotCount: 6,
    legacyRepositoryId: null,
};
//# sourceMappingURL=types.js.map