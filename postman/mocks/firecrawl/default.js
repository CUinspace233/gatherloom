// Local contract subset: https://docs.firecrawl.dev/api-reference/endpoint/scrape
// Only synthetic URLs and credentials are used. Never forward to the real API.
const http = require("http");
const bodyText = "# 网页正文\n\nSwiftUI 的 @Observable 宏用于状态管理。\n\n支持中文与 emoji 🌿。";

http.createServer((req, res) => {
  const send = (status, body) => {
    res.writeHead(status, { "Content-Type": "application/json" });
    res.end(JSON.stringify(body));
  };
  if (req.method !== "POST" || req.url !== "/v2/scrape") {
    return send(501, { success: false, error: "Not modeled" });
  }
  if (req.headers.authorization !== "Bearer test-secret") {
    return send(401, { success: false, error: "Invalid test key" });
  }
  const body = req.body;
  if (!body || JSON.stringify(body.formats) !== '["markdown"]' ||
      body.onlyMainContent !== true || body.storeInCache !== false || body.timeout !== 60000) {
    return send(400, { success: false, error: "Invalid scrape contract" });
  }
  let scenario;
  try { scenario = new URL(body.url).pathname.slice(1); }
  catch { return send(400, { success: false, error: "Invalid URL" }); }
  if (/^status-(401|402|403|429|503)$/.test(scenario)) {
    return send(Number(scenario.split("-")[1]), { success: false, error: "Synthetic error" });
  }
  if (scenario === "html-error") {
    res.writeHead(500, { "Content-Type": "text/html" });
    return res.end("<html>Bad gateway</html>");
  }
  if (scenario === "invalid-json") {
    res.writeHead(200, { "Content-Type": "application/json" });
    return res.end("invalid json");
  }
  const result = {
    success: true,
    data: {
      markdown: scenario === "empty" ? " \n" : scenario === "long" ? "x".repeat(100001) : bodyText,
      metadata: { title: "网页标题", statusCode: scenario === "page-403" ? 403 : 200 },
    },
  };
  if (scenario === "timeout") return setTimeout(() => send(200, result), 250);
  send(200, result);
}).listen(process.env.PORT);
