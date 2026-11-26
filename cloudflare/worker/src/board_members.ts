/**
 * 板块成分股（模块：板块详情）：数据由本地 Python 管道生成到
 * `reports/board_members.json`（GitHub 仓库主分支），Worker 读 GitHub raw 并做 60s 边缘缓存。
 *
 * 为什么走静态文件而不是 Worker 直连行情源：
 * - 东财 clist / 新浪成分股接口在 Cloudflare 出口容易被限流；
 * - 板块成分一天只需更新一次，静态化后 Worker 只读 GitHub raw（CDN 命中率高、稳定）。
 *
 * 端点（index.ts 路由）：
 *   GET /api/board-members                → 板块索引（不含 members，体积 ~5KB）
 *   GET /api/board-members?code=new_blhy  → 指定板块的完整成分股列表
 *
 * 本地开发可设环境变量 `BOARD_MEMBERS_URL` 指向本机文件服务（生产不要设置）；
 * 请求失败时优先返回进程内 last-known-good，避免上游抖动导致页面空窗。
 */

export const DEFAULT_BOARD_MEMBERS_URL =
  "https://raw.githubusercontent.com/xiongkong520/personal-quant/main/reports/board_members.json";

const CACHE_ORIGIN = "https://quant-edge-cache.local";
const CACHE_PATH = "/board-members.json";
const CACHE_TTL_SEC = 60;
const FETCH_TIMEOUT_MS = 15_000;
const USER_AGENT = "quant-worker/1.0";

export interface BoardMember {
  code: string;
  name: string;
  /** 最新价（元，不复权，与 /api/market-all 一致） */
  price: number;
  /** 当日涨跌幅（%） */
  pct_chg: number;
  /** 成交额（元） */
  amount: number;
}

export interface BoardDetail {
  code: string;
  name: string;
  /** 板块当日涨跌幅（%） */
  pct_chg: number;
  /** 成分股数量（= members.length） */
  count: number;
  members: BoardMember[];
}

export interface BoardMembersPayload {
  schema: number;
  generated_at: string;
  source: string;
  board_count: number;
  member_count: number;
  boards: BoardDetail[];
  data_quality?: Record<string, unknown>;
}

/** 本地 dev 可用 `BOARD_MEMBERS_URL` 覆盖数据地址（生产不配置）。 */
function boardMembersUrl(env: unknown): string {
  const override = (env as Record<string, unknown> | null | undefined)?.["BOARD_MEMBERS_URL"];
  return typeof override === "string" && override.trim() ? override.trim() : DEFAULT_BOARD_MEMBERS_URL;
}

/** 进程内 last-known-good：GitHub raw 抖动时兜底，避免页面空窗。 */
let lastKnownGood: BoardMembersPayload | null = null;

function normalizeMember(raw: unknown): BoardMember | null {
  if (!raw || typeof raw !== "object") return null;
  const row = raw as Record<string, unknown>;
  const code = String(row["code"] ?? "").trim();
  const name = String(row["name"] ?? "").trim();
  if (!/^\d{6}$/.test(code) || !name) return null;
  const num = (v: unknown): number => {
    const n = Number(v);
    return Number.isFinite(n) ? n : 0;
  };
  return { code, name, price: num(row["price"]), pct_chg: num(row["pct_chg"]), amount: num(row["amount"]) };
}

/** 校验并规整管道产物；结构异常直接抛错，避免把坏数据缓存/下发给前端。 */
export function normalizePayload(raw: unknown): BoardMembersPayload {
  if (!raw || typeof raw !== "object") throw new Error("board_members payload 不是对象");
  const obj = raw as Record<string, unknown>;
  const boardsRaw = obj["boards"];
  if (!Array.isArray(boardsRaw)) throw new Error("board_members payload 缺少 boards 数组");
  const boards: BoardDetail[] = [];
  for (const item of boardsRaw) {
    if (!item || typeof item !== "object") continue;
    const row = item as Record<string, unknown>;
    const code = String(row["code"] ?? "").trim();
    const name = String(row["name"] ?? "").trim();
    if (!/^[a-z0-9_]{2,32}$/i.test(code) || !name) continue;
    const membersRaw = row["members"];
    const members = Array.isArray(membersRaw)
      ? membersRaw.map(normalizeMember).filter((m): m is BoardMember => m !== null)
      : [];
    boards.push({
      code,
      name,
      pct_chg: Number.isFinite(Number(row["pct_chg"])) ? Number(row["pct_chg"]) : 0,
      count: members.length,
      members,
    });
  }
  if (boards.length === 0) throw new Error("board_members payload 无有效板块");
  const memberCount = boards.reduce((sum, b) => sum + b.members.length, 0);
  return {
    schema: Number.isFinite(Number(obj["schema"])) ? Number(obj["schema"]) : 1,
    generated_at: String(obj["generated_at"] ?? ""),
    source: String(obj["source"] ?? "sina"),
    board_count: boards.length,
    member_count: memberCount,
    boards,
    data_quality: (obj["data_quality"] as Record<string, unknown> | undefined) ?? undefined,
  };
}

/** 读板块成分数据：Cache API 60s → GitHub raw → 进程内 last-known-good。 */
export async function loadBoardMembersPayload(env: unknown): Promise<BoardMembersPayload> {
  const cacheKey = new Request(`${CACHE_ORIGIN}${CACHE_PATH}`, { method: "GET" });
  try {
    const hit = await caches.default.match(cacheKey);
    if (hit) return normalizePayload(await hit.json());
  } catch {
    /* Cache API 在 workers.dev 上可能 no-op，继续走网络 */
  }
  try {
    const resp = await fetch(boardMembersUrl(env), {
      headers: { "user-agent": USER_AGENT, accept: "application/json" },
      signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const payload = normalizePayload(await resp.json());
    lastKnownGood = payload;
    try {
      await caches.default.put(
        cacheKey,
        new Response(JSON.stringify(payload), {
          headers: {
            "content-type": "application/json; charset=utf-8",
            "cache-control": `public, max-age=${CACHE_TTL_SEC}`,
          },
        }),
      );
    } catch {
      /* 缓存写失败不影响本次返回 */
    }
    return payload;
  } catch (err) {
    if (lastKnownGood) {
      console.warn(`[board-members] 上游读取失败，使用 last-known-good：${err instanceof Error ? err.message : String(err)}`);
      return lastKnownGood;
    }
    throw err;
  }
}

/** 板块索引（不含 members）：前端拿 code ↔ name 映射，payload 只有几 KB。 */
export function boardIndexFromPayload(payload: BoardMembersPayload): Record<string, unknown> {
  return {
    schema: payload.schema,
    ok: true,
    generated_at: payload.generated_at,
    source: payload.source,
    board_count: payload.board_count,
    member_count: payload.member_count,
    boards: payload.boards.map((board) => ({
      code: board.code,
      name: board.name,
      pct_chg: board.pct_chg,
      count: board.count,
    })),
  };
}

/** 指定板块成分（code 大小写不敏感）；不存在返回 null。 */
export function boardDetailFromPayload(
  payload: BoardMembersPayload,
  code: string,
): Record<string, unknown> | null {
  const wanted = code.trim().toLowerCase();
  const board = payload.boards.find((item) => item.code.toLowerCase() === wanted);
  if (!board) return null;
  return {
    schema: payload.schema,
    ok: true,
    generated_at: payload.generated_at,
    source: payload.source,
    board: {
      code: board.code,
      name: board.name,
      pct_chg: board.pct_chg,
      count: board.count,
      members: board.members,
    },
  };
}
