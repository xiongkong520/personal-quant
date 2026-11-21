// Cloudflare Pages Functions 适配层：把 /api/* 交给现有 Worker 实现，
// 静态资源（/、/app.js、/styles.css）仍由 Pages 托管。
// 这样整站（页面 + API）同域运行在 https://<project>.pages.dev，
// 国内可访问，且不需要自定义域名。
import worker from "../../../worker/src/index";

interface PagesContext {
  request: Request;
  env: Record<string, unknown>;
  params: Record<string, string | string[]>;
  data: Record<string, unknown>;
  next: () => Promise<Response>;
  waitUntil: (promise: Promise<unknown>) => void;
  passThroughOnException: () => void;
}

export const onRequest = async (context: PagesContext): Promise<Response> => {
  // 只接管 API 路由；其余请求交给 Pages 静态资产（context.next()）
  return worker.fetch(context.request, context.env as never, context as never);
};
