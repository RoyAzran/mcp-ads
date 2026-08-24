/**
 * MarketingMCP CLI — local stdio MCP server.
 *
 * Registers ~10 category meta-tools (one per platform area) plus a single
 * `list_actions` discovery tool. Each tool call is forwarded to the
 * MarketingMCP backend at MCP_ADS_BASE_URL using the user's API key.
 *
 * The agent sees ~11 tool definitions instead of ~1000, dramatically
 * reducing the token cost of the tool list.
 */
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
  type Tool,
} from "@modelcontextprotocol/sdk/types.js";

const VERSION = "0.1.0";

// ───────────────────────────────────────────────────────────────────────────
// Config (CLI flags + env)
// ───────────────────────────────────────────────────────────────────────────

interface Config {
  apiKey: string;
  baseUrl: string;
}

function parseConfig(argv: string[]): Config {
  let apiKey = process.env.MCP_ADS_API_KEY ?? "";
  let baseUrl = process.env.MCP_ADS_BASE_URL ?? "https://mcp-ads.com";

  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--api-key" || arg === "-k") {
      apiKey = argv[++i] ?? "";
    } else if (arg.startsWith("--api-key=")) {
      apiKey = arg.slice("--api-key=".length);
    } else if (arg === "--base-url") {
      baseUrl = argv[++i] ?? baseUrl;
    } else if (arg.startsWith("--base-url=")) {
      baseUrl = arg.slice("--base-url=".length);
    } else if (arg === "--version" || arg === "-v") {
      process.stdout.write(`@marketingmcp/cli ${VERSION}\n`);
      process.exit(0);
    } else if (arg === "--help" || arg === "-h") {
      printHelp();
      process.exit(0);
    }
  }

  // Strip stray quotes/whitespace
  apiKey = apiKey.trim().replace(/^['"]|['"]$/g, "");
  baseUrl = baseUrl.trim().replace(/\/+$/, "");

  return { apiKey, baseUrl };
}

function printHelp(): void {
  process.stdout.write(
    [
      `MarketingMCP CLI v${VERSION}`,
      ``,
      `A local stdio MCP server that proxies tool calls to your MarketingMCP account.`,
      `Use it with Claude Code, Cursor, Cline, Continue, Windsurf, or any MCP-compatible agent.`,
      ``,
      `Usage:`,
      `  npx -y @marketingmcp/cli [options]`,
      ``,
      `Options:`,
      `  --api-key, -k <key>    Your MarketingMCP API key (or set MCP_ADS_API_KEY env)`,
      `  --base-url <url>       Backend URL (default: https://mcp-ads.com,`,
      `                         or set MCP_ADS_BASE_URL env)`,
      `  --version, -v          Print version and exit`,
      `  --help, -h             Print this help and exit`,
      ``,
      `Get an API key at https://mcp-ads.com/manage`,
      ``,
    ].join("\n"),
  );
}

// ───────────────────────────────────────────────────────────────────────────
// HTTP backend client
// ───────────────────────────────────────────────────────────────────────────

interface ManifestCategory {
  category: string;
  tool_count: number;
  description: string;
}

interface Manifest {
  version: number;
  user: { id: string; email: string };
  categories: ManifestCategory[];
  discovery_tool: { name: string; description: string };
}

class Backend {
  constructor(private readonly cfg: Config) {}

  private async request<T>(path: string, body: unknown): Promise<T> {
    const url = `${this.cfg.baseUrl}${path}`;
    const method = body === undefined ? "GET" : "POST";
    const init: RequestInit = {
      method,
      headers: {
        Authorization: `Bearer ${this.cfg.apiKey}`,
        "User-Agent": `marketingmcp-cli/${VERSION}`,
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
    };
    if (body !== undefined) {
      init.body = JSON.stringify(body);
    }
    const res = await fetch(url, init);
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try {
        const data = (await res.json()) as { detail?: string };
        if (data && typeof data.detail === "string") detail = data.detail;
      } catch {
        try {
          detail = (await res.text()) || detail;
        } catch {
          // fall through
        }
      }
      throw new BackendError(res.status, detail);
    }
    return (await res.json()) as T;
  }

  async manifest(): Promise<Manifest> {
    return this.request<Manifest>("/api/cli/manifest", undefined);
  }

  async listActions(category: string, search: string, limit = 200, offset = 0) {
    return this.request<{
      category: string;
      total: number;
      returned: number;
      offset: number;
      actions: Array<{ name: string; description: string; parameters: unknown }>;
    }>("/api/cli/list_actions", { category, search, limit, offset });
  }

  async dispatch(category: string, action: string, params: Record<string, unknown>) {
    return this.request<{ category: string; action: string; result: unknown }>(
      "/api/cli/dispatch",
      { category, action, params },
    );
  }
}

class BackendError extends Error {
  constructor(public readonly status: number, message: string) {
    super(message);
    this.name = "BackendError";
  }
}

// ───────────────────────────────────────────────────────────────────────────
// Tool definitions exposed to the agent
// ───────────────────────────────────────────────────────────────────────────

function buildTools(manifest: Manifest): Tool[] {
  const tools: Tool[] = [];

  // Per-category meta-tool. The agent invokes this with (action, params).
  for (const cat of manifest.categories) {
    tools.push({
      name: cat.category,
      description:
        `${cat.description}\n\n` +
        `This tool routes to one of ${cat.tool_count} underlying actions. ` +
        `Set 'action' to the action name. Use list_actions(category="${cat.category}") ` +
        `first if you don't know the exact action name or its parameters.`,
      inputSchema: {
        type: "object",
        properties: {
          action: {
            type: "string",
            description:
              `The exact action name to run (e.g. discovered via list_actions ` +
              `with category="${cat.category}").`,
          },
          params: {
            type: "object",
            description: "Action parameters as a JSON object.",
            additionalProperties: true,
          },
        },
        required: ["action"],
      },
    });
  }

  // Discovery tool — lists actions inside a category, optionally filtered.
  tools.push({
    name: "list_actions",
    description:
      manifest.discovery_tool.description ||
      "List actions available within a category. Always call this before invoking a category tool when you don't already know the action name.",
    inputSchema: {
      type: "object",
      properties: {
        category: {
          type: "string",
          enum: manifest.categories.map((c) => c.category),
          description: "The category to list actions for.",
        },
        search: {
          type: "string",
          description: "Optional case-insensitive substring filter on action name or description.",
        },
        limit: { type: "number", description: "Max actions to return (default 200, max 500)." },
        offset: { type: "number", description: "Skip this many actions (default 0)." },
      },
      required: ["category"],
    },
  });

  return tools;
}

// ───────────────────────────────────────────────────────────────────────────
// MCP server bootstrap
// ───────────────────────────────────────────────────────────────────────────

async function main(): Promise<void> {
  const cfg = parseConfig(process.argv.slice(2));

  if (!cfg.apiKey) {
    process.stderr.write(
      "[marketingmcp] No API key provided.\n" +
        "  Set MCP_ADS_API_KEY env var, or pass --api-key <key>.\n" +
        "  Get a key at https://mcp-ads.com/manage\n",
    );
    process.exit(1);
  }

  const backend = new Backend(cfg);

  // Fetch the manifest at startup. Surface errors clearly so a misconfigured
  // key fails fast instead of returning empty tool lists to the agent.
  let manifest: Manifest;
  try {
    manifest = await backend.manifest();
  } catch (e) {
    if (e instanceof BackendError) {
      process.stderr.write(`[marketingmcp] Could not fetch tool manifest (${e.status}): ${e.message}\n`);
    } else {
      process.stderr.write(`[marketingmcp] Could not reach ${cfg.baseUrl}: ${(e as Error).message}\n`);
    }
    process.exit(1);
  }

  const tools = buildTools(manifest);

  process.stderr.write(
    `[marketingmcp] Connected as ${manifest.user.email}. ` +
      `Registered ${tools.length} tools (${manifest.categories.length} categories ` +
      `routing to ${manifest.categories.reduce((s, c) => s + c.tool_count, 0)} underlying actions).\n`,
  );

  const server = new Server(
    { name: "marketingmcp", version: VERSION },
    { capabilities: { tools: {} } },
  );

  server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools }));

  server.setRequestHandler(CallToolRequestSchema, async (req) => {
    const name = req.params.name;
    const args = (req.params.arguments ?? {}) as Record<string, unknown>;

    try {
      if (name === "list_actions") {
        const category = String(args.category ?? "");
        const search = String(args.search ?? "");
        const limit = typeof args.limit === "number" ? (args.limit as number) : 200;
        const offset = typeof args.offset === "number" ? (args.offset as number) : 0;
        const data = await backend.listActions(category, search, limit, offset);
        return {
          content: [{ type: "text", text: JSON.stringify(data, null, 2) }],
        };
      }

      // Otherwise: this must be a category meta-tool call.
      const knownCategory = manifest.categories.find((c) => c.category === name);
      if (!knownCategory) {
        throw new Error(
          `Unknown tool: ${name}. Known: ${[
            ...manifest.categories.map((c) => c.category),
            "list_actions",
          ].join(", ")}`,
        );
      }
      const action = String(args.action ?? "");
      if (!action) {
        throw new Error(
          `Missing 'action'. Call list_actions(category="${name}") to discover available actions.`,
        );
      }
      const params = (args.params ?? {}) as Record<string, unknown>;
      const data = await backend.dispatch(name, action, params);
      return {
        content: [{ type: "text", text: JSON.stringify(data.result, null, 2) }],
      };
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      return {
        isError: true,
        content: [{ type: "text", text: msg }],
      };
    }
  });

  const transport = new StdioServerTransport();
  await server.connect(transport);

  // Stay alive — the SDK keeps the event loop busy as long as stdin is open.
  process.on("SIGINT", () => process.exit(0));
  process.on("SIGTERM", () => process.exit(0));
}

main().catch((e) => {
  process.stderr.write(`[marketingmcp] Fatal: ${(e as Error).message}\n`);
  process.exit(1);
});
